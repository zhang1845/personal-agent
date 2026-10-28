"""Small atomic, versioned 0600 settings store with redacted secret fields."""
import json
import os
import tempfile
import threading
from pathlib import Path
from .errors import AgentError
from .platform_utils import private_file_permissions

class PrivateConfig:
    def __init__(self,path,defaults,secrets=()):
        self.path=Path(path);self.defaults=dict(defaults,version=1);self.secrets=set(secrets);self.lock=threading.RLock()
    def read(self):
        with self.lock:
            if not self.path.exists():return dict(self.defaults)
            try:
                if self.path.is_symlink() or self.path.stat().st_size>65536:raise ValueError()
                value=json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(value,dict) or type(value.get('version')) is not int:raise ValueError()
                return dict(self.defaults,**value)
            except (OSError,ValueError):raise AgentError('storage_unavailable','私密配置无法读取') from None
    def public(self):
        value=self.read()
        for key in self.secrets:value['has_'+key]=bool(value.pop(key,''))
        return value
    def save(self,body,validate):
        if not isinstance(body,dict) or set(body)-set(self.defaults)-{'expected_version'}-{'clear_'+key for key in self.secrets} or 'version' in body:raise AgentError('validation_error','配置字段无效')
        with self.lock:
            old=self.read()
            if type(body.get('expected_version')) is not int or body['expected_version']!=old['version']:raise AgentError('version_conflict','配置已变化，请重新加载')
            value=dict(old)
            for key in self.defaults:
                if key!='version' and key in body:value[key]=body[key]
            for key in self.secrets:
                clear=body.get('clear_'+key,False)
                if type(clear) is not bool:raise AgentError('validation_error','清除标记无效')
                if clear and body.get(key):raise AgentError('validation_error','不能同时设置和清除凭据')
                value[key]='' if clear else body.get(key) or old[key]
            validate(value,old,body);value['version']+=1;self._write(value);return self.public()
    def _write(self,value):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.is_symlink():raise AgentError('storage_unavailable','配置路径不可为链接')
        fd,name=tempfile.mkstemp(prefix='.private-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as out:
                private_file_permissions(out.fileno());json.dump(value,out,ensure_ascii=False,allow_nan=False);out.flush();os.fsync(out.fileno())
            os.replace(name,self.path)
        finally:
            if os.path.exists(name):os.unlink(name)
