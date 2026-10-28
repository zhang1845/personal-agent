"""Opt-in Chat Completions configuration and server-side client; stdlib only."""
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import tempfile
from .platform_utils import private_file_permissions
import threading
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

from .errors import AgentError
from .ollama_client import _NoRedirect


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def base_url(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise AgentError('validation_error', 'API 地址格式无效')
    value = value.strip().rstrip('/')
    if not value:
        return ''
    try:
        parts = urlsplit(value)
        if parts.port is not None and not 1 <= parts.port <= 65535: raise ValueError()
        if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError()
        if any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value: raise ValueError()
        if parts.scheme == 'http':
            host = parts.hostname.lower()
            if host != 'localhost' and not ipaddress.ip_address(host).is_loopback: raise ValueError()
        parts.hostname.encode('ascii')
    except (ValueError, UnicodeError):
        raise AgentError('validation_error', 'API 地址须为 HTTPS；仅本机地址允许 HTTP，不支持账号、查询参数或片段') from None
    path = parts.path.rstrip('/')
    if path.endswith('/chat/completions'): path = path[:-len('/chat/completions')]
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def model_name(value):
    if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AgentError('validation_error', '模型名称格式无效，最多 200 字')
    return value.strip()


def api_key(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise AgentError('validation_error', 'API Key 格式无效')
    value = value.strip()
    if any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise AgentError('validation_error', 'API Key 不能包含空白或非 ASCII 字符')
    return value


class APISettings:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _read(self):
        if not self.path.exists():
            return {'base_url':'', 'model':'', 'api_key':'', 'enabled':False, 'version':1, 'last_test':None}
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 16384: raise ValueError()
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] < 1 or type(data.get('enabled')) is not bool: raise ValueError()
            data['base_url'] = base_url(data.get('base_url', ''))
            data['model'] = model_name(data.get('model', ''))
            data['api_key'] = api_key(data.get('api_key', ''))
            self._complete(data)
            return data
        except (OSError, ValueError, TypeError, AgentError):
            raise AgentError('storage_unavailable', 'API 配置无法读取；未尝试连接模型，请检查本机配置文件') from None

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            if self.path.is_symlink(): raise OSError()
            fd, temporary = tempfile.mkstemp(prefix='.model-api-', dir=str(self.path.parent))
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                private_file_permissions(stream.fileno())
                json.dump(data, stream, ensure_ascii=False, allow_nan=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, str(self.path))
        except OSError:
            raise AgentError('storage_unavailable', 'API 配置未能保存，请检查本机目录权限') from None
        finally:
            if temporary and os.path.exists(temporary): os.unlink(temporary)

    @staticmethod
    def _complete(data):
        if data['enabled'] and (not data['base_url'] or not data['model']):
            raise AgentError('validation_error', '启用 API 前请填写地址和模型名称')

    @staticmethod
    def public(data):
        return {key:data.get(key) for key in ('base_url','model','enabled','version','last_test')} | {'has_key':bool(data['api_key']), 'protocol':'chat_completions'}

    def snapshot(self):
        with self.lock: return dict(self._read())

    def get(self): return self.public(self.snapshot())

    def save(self, body):
        allowed = {'base_url','model','api_key','clear_key','enabled','expected_version'}
        if not isinstance(body, dict) or set(body)-allowed or not {'base_url','model','enabled','expected_version'} <= set(body):
            raise AgentError('validation_error', 'API 配置字段缺失或不受支持')
        if type(body['expected_version']) is not int or body['expected_version'] < 1 or type(body['enabled']) is not bool or type(body.get('clear_key',False)) is not bool:
            raise AgentError('validation_error', '配置版本或启用状态无效')
        address, model, key = base_url(body['base_url']), model_name(body['model']), api_key(body.get('api_key',''))
        if key and body.get('clear_key'): raise AgentError('validation_error', '请在更新 Key 和清除 Key 中选择一项')
        with self.lock:
            current = self._read()
            if current['version'] != body['expected_version']: raise AgentError('version_conflict', 'API 配置已变化，请重新加载后保存')
            if current['api_key'] and address != current['base_url'] and not key and not body.get('clear_key'):
                raise AgentError('validation_error', '更换 API 地址时请重新填写 Key，或勾选清除已保存的 Key')
            data = {'base_url':address, 'model':model, 'enabled':body['enabled'], 'api_key': '' if body.get('clear_key') else key or current['api_key'], 'version':current['version']+1, 'last_test':None}
            if all(data[key] == current[key] for key in ('base_url','model','api_key')):
                data['last_test'] = current.get('last_test')
            self._complete(data); self._write(data)
            return self.public(data)

    def record_test(self, expected_version, ok, code):
        with self.lock:
            data = self._read()
            if data['version'] != expected_version: raise AgentError('version_conflict', '测试期间配置已变化，请测试当前配置')
            data['last_test'] = {'ok':bool(ok), 'code':code, 'at':timestamp()}
            self._write(data)
            return self.public(data)


class APIModelClient:
    MAX_REQUEST = 512 * 1024
    MAX_RESPONSE = 2 * 1024 * 1024

    def __init__(self, config, opener=None, timeout=30):
        self.config = dict(config)
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self.timeout = timeout

    @staticmethod
    def messages(messages):
        """Convert Ollama-style tool envelopes to standard call IDs and JSON arguments."""
        if not isinstance(messages, list) or not 1 <= len(messages) <= 60: raise AgentError('validation_error', '模型消息数量无效')
        output, pending, seen_ids = [], [], set()
        for item in messages:
            if not isinstance(item, dict) or item.get('role') not in ('system','user','assistant','tool') or not isinstance(item.get('content',''), str):
                raise AgentError('validation_error', '模型消息格式无效')
            msg = {'role':item['role'], 'content':item.get('content','')}
            if item.get('tool_calls'):
                if item['role'] != 'assistant' or not isinstance(item['tool_calls'],list) or len(item['tool_calls'])>8: raise AgentError('validation_error','工具调用格式无效')
                calls = []
                for call in item['tool_calls']:
                    if not isinstance(call,dict) or not isinstance(call.get('function'),dict): raise AgentError('validation_error','工具调用格式无效')
                    function = call['function']
                    name = function.get('name'); arguments = function.get('arguments',{})
                    if not isinstance(name,str) or not name: raise AgentError('validation_error','工具名称无效')
                    if isinstance(arguments,str):
                        try: arguments = json.loads(arguments)
                        except ValueError: raise AgentError('validation_error','工具参数须为 JSON 对象') from None
                    if not isinstance(arguments,dict): raise AgentError('validation_error','工具参数须为对象')
                    call_id = call.get('id') or 'workbench-call-'+str(len(seen_ids)+1)
                    if not isinstance(call_id,str) or not call_id or call_id in seen_ids: raise AgentError('validation_error','工具调用 ID 重复或无效')
                    seen_ids.add(call_id); pending.append((call_id,name))
                    calls.append({'id':call_id,'type':'function','function':{'name':name,'arguments':json.dumps(arguments,ensure_ascii=False)}})
                msg['tool_calls'] = calls
            if item['role'] == 'tool':
                match = next(((i,p) for i,p in enumerate(pending) if p[0] == item.get('tool_call_id') or (not item.get('tool_call_id') and p[1] == item.get('tool_name'))),None)
                if not match: raise AgentError('validation_error','工具结果缺少对应调用')
                i,(call_id,_) = match;pending.pop(i);msg['tool_call_id'] = call_id
            output.append(msg)
        return output

    def chat(self, messages, tools, model, test=False):
        address = base_url(self.config.get('base_url','')); configured_model = model_name(self.config.get('model',''))
        if not address or not configured_model: raise AgentError('validation_error','请先保存 API 地址和模型名称')
        if model != configured_model: raise AgentError('validation_error','模型配置已变化，请刷新后重试')
        if not test and not self.config.get('enabled'): raise AgentError('model_unavailable','API 尚未启用')
        payload = {'model':configured_model,'messages':self.messages(messages),'stream':False,'max_tokens':16 if test else 1200}
        if tools: payload['tools'] = tools
        try:
            raw = json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8')
            if len(raw)>self.MAX_REQUEST: raise AgentError('validation_error','模型上下文过大，请缩短输入或开始新对话')
            headers={'Content-Type':'application/json','Accept':'application/json'}
            if self.config.get('api_key'): headers['Authorization']='Bearer '+api_key(self.config['api_key'])
            request=Request(address+'/chat/completions',data=raw,headers=headers,method='POST')
            with self.opener.open(request,timeout=self.timeout) as response: data=response.read(self.MAX_RESPONSE+1)
            if len(data)>self.MAX_RESPONSE: raise AgentError('model_invalid_response','API 响应超过大小上限')
            result=json.loads(data.decode('utf-8'));choices=result.get('choices')
            if not isinstance(choices,list) or not choices: raise ValueError()
            message=choices[0].get('message')
            if not isinstance(message,dict) or message.get('role','assistant')!='assistant': raise ValueError()
            content=message.get('content') or ''
            if not isinstance(content,str): raise ValueError()
            calls=message.get('tool_calls',[])
            if not isinstance(calls,list) or len(calls)>8: raise ValueError()
            for call in calls:
                if not isinstance(call,dict) or not isinstance(call.get('id'),str) or not isinstance(call.get('function'),dict): raise ValueError()
            if not content.strip() and not calls: raise ValueError()
            usage=result.get('usage',{})
            self.last_usage={k:v for k,v in usage.items() if k in ('prompt_tokens','completion_tokens','total_tokens') and type(v) is int and v>=0} if isinstance(usage,dict) else {}
            return {'role':'assistant','content':content,**({'tool_calls':calls} if calls else {})}
        except AgentError: raise
        except HTTPError as error:
            messages_by_code={401:'API Key 无效或未授权',403:'当前 API Key 没有访问权限',404:'API 地址或模型不存在',429:'API 请求受限，请检查额度或频率'}
            try: error.close()
            except (OSError, KeyError, AttributeError): pass
            raise AgentError('model_unavailable',messages_by_code.get(error.code,'API 请求失败（HTTP '+str(error.code)+'）')+'；未切换到其他服务') from None
        except (URLError,OSError,TimeoutError):
            raise AgentError('model_unavailable','API 连接失败或超时，请检查地址和网络；未自动重试或切换服务') from None
        except (ValueError,TypeError,AttributeError,UnicodeError,RecursionError):
            raise AgentError('model_invalid_response','API 未返回有效的 Chat Completions 结果，请检查协议和模型名称') from None

    def test(self):
        self.chat([{'role':'user','content':'Reply OK.'}],[],self.config['model'],test=True)
        return {'ok':True,'message':'接口与所选模型已响应；工具调用能力尚未验证。'}
