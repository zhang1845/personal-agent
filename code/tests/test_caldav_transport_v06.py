"""Exercise the installed python-caldav transport against a loopback DAV server."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import base64
import threading
from xml.sax.saxutils import escape
from test_v06_features import Base,remote_ics
from personal_agent.caldav_sync import CalDAVSync

class RealTransportTests(Base):
 def test_real_apprise_delivery_to_explicit_loopback_webhook(self):
  from personal_agent.automations import Automations
  received=[]
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_POST(self):
    received.append(self.rfile.read(int(self.headers['Content-Length'])));self.send_response(200);self.end_headers()
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
  try:
   auto=Automations(self.root,self.calendar,self.store)
   auto.save_settings({'urls':'json://127.0.0.1:'+str(server.server_port)+'/hook','enabled':True,'expected_version':1})
   result=auto.test_notification();auto.drain();self.assertEqual('sent',result['notification']['state']);self.assertEqual(1,len(received))
  finally:server.shutdown();server.server_close();worker.join()
 def test_installed_client_discovery_import_and_conditional_put(self):
  records={'/cal/main/remote.ics':(remote_ics(),'"1"')};observed=[]
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def read_body(self):return self.rfile.read(int(self.headers.get('Content-Length',0))).decode()
   def send(self,status,body='',kind='application/xml',etag=None):
    raw=body.encode();self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)))
    if etag:self.send_header('ETag',etag)
    self.end_headers();self.wfile.write(raw)
   def auth(self):
    observed.append((self.command,self.path,self.headers.get('Authorization'),self.headers.get('If-Match'),self.headers.get('If-None-Match')))
    return self.headers.get('Authorization')=='Basic '+base64.b64encode(b'user:pass').decode()
   def multi(self,responses):self.send(207,'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'+responses+'</D:multistatus>')
   def response(self,path,props):return '<D:response><D:href>'+path+'</D:href><D:propstat><D:prop>'+props+'</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
   def do_PROPFIND(self):
    body=self.read_body()
    if not self.auth():return self.send(403)
    if 'current-user-principal' in body:return self.multi(self.response(self.path,'<D:current-user-principal><D:href>/principal/</D:href></D:current-user-principal>'))
    if 'calendar-home-set' in body:return self.multi(self.response(self.path,'<C:calendar-home-set><D:href>/cal/</D:href></C:calendar-home-set>'))
    self.multi(self.response('/cal/','<D:resourcetype><D:collection/></D:resourcetype>')+self.response('/cal/main/','<D:resourcetype><D:collection/><C:calendar/></D:resourcetype><D:displayname>Loopback calendar</D:displayname><C:supported-calendar-component-set><C:comp name="VEVENT"/></C:supported-calendar-component-set>'))
   def do_REPORT(self):
    self.read_body()
    if not self.auth():return self.send(403)
    self.multi(''.join(self.response(path,'<D:getetag>'+escape(etag)+'</D:getetag><C:calendar-data>'+escape(body)+'</C:calendar-data>') for path,(body,etag) in records.items()))
   def do_GET(self):
    if not self.auth():return self.send(403)
    if self.path not in records:return self.send(404)
    raw,etag=records[self.path];self.send(200,raw,'text/calendar',etag)
   def do_PUT(self):
    raw=self.read_body()
    if not self.auth():return self.send(403)
    current=records.get(self.path)
    if current and self.headers.get('If-Match')!=current[1]:return self.send(412)
    if not current and self.headers.get('If-None-Match')!='*':return self.send(412)
    records[self.path]=(raw,'"2"');self.send(204)
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
  try:
   origin='http://127.0.0.1:'+str(server.server_port)
   sync=CalDAVSync(self.root,self.calendar)
   sync.save_settings({'url':origin+'/dav/','username':'user','password':'pass','calendar_url':origin+'/cal/main/','enabled':True,'expected_version':1})
   self.assertEqual(origin+'/cal/main/',sync.test()['calendars'][0]['url'])
   self.assertEqual(1,sync.sync('2030-01-01','2030-02-01')['imported'])
   sync.save_settings({'writes_enabled':True,'expected_version':2});self.event()
   result=sync.sync('2030-01-01','2030-02-01');self.assertTrue(all(o['status']=='applied' for o in result['operations']));self.assertEqual(2,len(records));self.assertTrue(any(method=='PUT' and none=='*' for method,path,auth,match,none in observed))
  finally:server.shutdown();server.server_close();worker.join()
