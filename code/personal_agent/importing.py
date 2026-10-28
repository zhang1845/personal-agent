"""Preview-only article and document import; saving remains a separate user action."""
import base64
import hashlib
import http.client
import io
import ipaddress
import json
import re
import socket
import ssl
import zipfile
from urllib.parse import urlsplit,urljoin,urlunsplit,urlencode
from .errors import AgentError

MAX_FILE=8*1024*1024
MAX_TEXT=12000


def _public_dns(host):
    """Opt-in DoH only for proxy fake IPs; pin resolver IP and verify its TLS name."""
    connection=http.client.HTTPSConnection('cloudflare-dns.com',timeout=5)
    def connect():connection.sock=ssl.create_default_context().wrap_socket(socket.create_connection(('1.1.1.1',443),timeout=5),server_hostname='cloudflare-dns.com')
    connection.connect=connect
    try:
        connection.request('GET','/dns-query?'+urlencode({'name':host.encode('idna').decode(),'type':'A'}),headers={'Accept':'application/dns-json'})
        response=connection.getresponse();raw=response.read(16385)
        if response.status!=200 or len(raw)>16384:raise ValueError()
        data=json.loads(raw)
        if data.get('Status')!=0:raise ValueError()
        addresses=list(dict.fromkeys(r['data'] for r in data.get('Answer',[]) if r.get('type')==1))
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):raise ValueError()
        return addresses
    except (OSError,ValueError,TypeError,KeyError,http.client.HTTPException):raise AgentError('import_failed','公开 DNS 未返回可用的公开地址，未读取网页') from None
    finally:connection.close()

def public_target(url,public_dns=False):
    try:
        if not isinstance(url,str) or len(url)>2000 or any(ord(c)<=32 for c in url):raise ValueError()
        p=urlsplit(url)
        if p.scheme not in ('http','https') or not p.hostname or p.username is not None or p.password is not None:raise ValueError()
        if type(public_dns) is not bool:raise ValueError()
        try:literal=ipaddress.ip_address(p.hostname)
        except ValueError:literal=None
        if literal and not literal.is_global:raise ValueError()
        port=p.port or (443 if p.scheme=='https' else 80)
        if port not in (80,443):raise ValueError()
        addresses=list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(p.hostname,port,type=socket.SOCK_STREAM)))
        if public_dns and addresses and all(ipaddress.ip_address(a) in ipaddress.ip_network('198.18.0.0/15') for a in addresses):addresses=_public_dns(p.hostname)
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):raise AgentError('validation_error','网页域名解析到内网或代理假 IP，已停止抓取。请检查 DNS/代理设置，或改用文件导入')
        return p,addresses[0],port
    except (ValueError,OSError):raise AgentError('validation_error','链接仅允许公开 HTTP/HTTPS 网页，不读取本机、内网或特殊端口') from None


def fetch_article(url,public_dns=False):
    for _ in range(4):
        p,ip,port=public_target(url,public_dns)
        connection=http.client.HTTPSConnection(p.hostname,port,timeout=15,context=ssl.create_default_context()) if p.scheme=='https' else http.client.HTTPConnection(p.hostname,port,timeout=15)
        def connect():
            sock=socket.create_connection((ip,port),timeout=15)
            connection.sock=ssl.create_default_context().wrap_socket(sock,server_hostname=p.hostname) if p.scheme=='https' else sock
        connection.connect=connect
        try:
            connection.request('GET',urlunsplit(('','',p.path or '/',p.query,'')),headers={'User-Agent':'PersonalAgent/0.6 article preview','Accept':'text/html,text/plain','Accept-Encoding':'identity'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location:raise AgentError('import_failed','网页重定向缺少目标')
                url=urljoin(url,location);continue
            if response.status!=200:raise AgentError('import_failed','网页无法读取（HTTP %s），可能需要登录或限制抓取'%response.status)
            kind=response.getheader('Content-Type','').lower()
            if not any(x in kind for x in ('text/html','text/plain','application/xhtml+xml')):raise AgentError('import_failed','链接未返回可读网页，请使用文件导入')
            if response.getheader('Content-Encoding','identity') not in ('','identity'):raise AgentError('import_failed','网页编码暂不支持')
            raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024:raise AgentError('import_failed','网页超过 2 MB 导入限制')
            return raw,url
        except (OSError,http.client.HTTPException):raise AgentError('import_failed','网页连接失败或超时，未保存资料') from None
        finally:connection.close()
    raise AgentError('import_failed','网页重定向次数过多')


class ImportService:
    def __init__(self,library,fetcher=None):self.library=library;self.fetcher=fetcher or fetch_article
    def result(self,title,content,url='',filename=''):
        if not isinstance(content,str) or not content.strip():raise AgentError('import_failed','未提取到可读正文；扫描件、加密文件和 OCR 需单独处理')
        content=content.strip();truncated=len(content)>MAX_TEXT
        # Preview is explicit about the limit; never silently save a partial document.
        shown=content[:MAX_TEXT]
        with self.library.connection() as c:
            rows=c.execute('SELECT id,title,content,url FROM sources').fetchall()
        duplicates=[{'id':r['id'],'title':r['title']} for r in rows if (url and r['url']==url) or r['content']==shown]
        return {'title':title[:200] or '未命名资料','content':shown,'url':url,'filename':filename,'characters':len(content),'truncated':truncated,'warning':'正文超过 12000 字，预览已截选；请明确选择要保存的内容，原文件未保存。' if truncated else '', 'duplicates':duplicates[:10], 'sha256':hashlib.sha256(content.encode()).hexdigest()}
    def article(self,url,public_dns=False):
        if type(public_dns) is not bool:raise AgentError('validation_error','公开 DNS 选项无效')
        try:
            import trafilatura
            raw,final=self.fetcher(url,public_dns=True) if public_dns else self.fetcher(url)
            value=trafilatura.bare_extraction(raw,with_metadata=True,include_comments=False,include_tables=True)
            if not value:raise AgentError('import_failed','未提取到正文，可能是登录页或需要浏览器执行脚本')
            title=getattr(value,'title',None) or '网页资料';content=getattr(value,'text',None)
            if content is None and isinstance(value,dict):title=value.get('title') or title;content=value.get('text')
            return self.result(title,content,final)
        except ImportError:raise AgentError('dependency_missing','请用项目 .venv 启动以启用网页提取') from None
    def document(self,filename,encoded):
        if not isinstance(filename,str) or len(filename)>200 or '/' in filename or '\\' in filename:raise AgentError('validation_error','文件名无效')
        ext='.'+filename.rsplit('.',1)[-1].lower()
        if ext not in ('.pdf','.docx','.pptx','.xlsx','.txt','.md'):raise AgentError('validation_error','支持 PDF、DOCX、PPTX、XLSX、TXT、Markdown')
        try:
            raw=base64.b64decode(encoded,validate=True)
            if not raw or len(raw)>MAX_FILE:raise ValueError()
        except (ValueError,TypeError):raise AgentError('validation_error','文件无效或超过 8 MB') from None
        if ext=='.pdf' and not raw.startswith(b'%PDF-'):raise AgentError('validation_error','文件不是有效 PDF')
        if ext in ('.docx','.pptx','.xlsx'):
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    entries=archive.infolist()
                    if len(entries)>2000 or sum(e.file_size for e in entries)>32*1024*1024 or any(e.flag_bits&1 for e in entries):raise ValueError()
                    if '[Content_Types].xml' not in archive.namelist():raise ValueError()
            except (ValueError,zipfile.BadZipFile):raise AgentError('validation_error','Office 文件无效、加密或解压后过大') from None
        try:
            if ext in ('.txt','.md'):content=raw.decode('utf-8-sig')
            else:
                from markitdown import StreamInfo
                from markitdown.converters import PdfConverter,DocxConverter,PptxConverter,XlsxConverter
                converter={".pdf":PdfConverter,".docx":DocxConverter,".pptx":PptxConverter,".xlsx":XlsxConverter}[ext]()
                result=converter.convert(io.BytesIO(raw),stream_info=StreamInfo(extension=ext))
                content=result.text_content
            return self.result(filename.rsplit('.',1)[0],content,filename=filename)
        except ImportError:raise AgentError('dependency_missing','请用项目 .venv 启动以启用文档解析') from None
        except AgentError:raise
        except Exception:raise AgentError('import_failed','文件解析失败；请检查格式、密码或是否为扫描件，未保存资料') from None
