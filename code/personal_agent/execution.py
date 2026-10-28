"""Visible execution history, containing tool names/results status, never API keys."""
import json
from pathlib import Path
from uuid import uuid4
from .personal_store import PersonalStore,now

class ExecutionLog(PersonalStore):
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as c:
            c.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session_id TEXT,engine TEXT,model TEXT,state TEXT,created_at TEXT,finished_at TEXT,error TEXT,usage TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS steps(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,name TEXT,state TEXT,created_at TEXT)')
            c.execute("UPDATE runs SET state='interrupted',finished_at=? WHERE state='running'",(now(),))
    def start(self,session_id,engine,model):
        rid=uuid4().hex
        with self.connection() as c:c.execute("INSERT INTO runs VALUES(?,?,?,?,'running',?,NULL,NULL,NULL)",(rid,session_id,engine,model,now()))
        return rid
    def step(self,rid,name,state):
        with self.connection() as c:c.execute('INSERT INTO steps(run_id,name,state,created_at) VALUES(?,?,?,?)',(rid,name,state,now()))
    def finish(self,rid,state='completed',error=None,usage=None):
        with self.connection() as c:c.execute('UPDATE runs SET state=?,finished_at=?,error=?,usage=? WHERE id=?',(state,now(),error,json.dumps(usage) if usage else None,rid))
    def list(self,session_id=None):
        with self.connection() as c:
            rows=c.execute('SELECT * FROM runs'+(' WHERE session_id=?' if session_id else '')+' ORDER BY created_at DESC LIMIT 30',(session_id,) if session_id else ()).fetchall();out=[]
            for row in rows:
                value=dict(row);value['usage']=json.loads(value['usage']) if value['usage'] else None;value['steps']=[dict(r) for r in c.execute('SELECT name,state,created_at FROM steps WHERE run_id=? ORDER BY id',(row['id'],))];out.append(value)
            return out
