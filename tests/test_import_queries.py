import os,sqlite3,tempfile

from app import main, llm


def _make_import_db_bytes(table='import_items', rows=None):
    if rows is None:
        rows = [(1, 'imported')]
    fd,path=tempfile.mkstemp(suffix='.db'); os.close(fd)
    conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE ' + table + ' (id INTEGER,label TEXT)')
    for r in rows:
        conn.execute('INSERT INTO ' + table + ' VALUES (?,?)', (r[0], r[1]))
    conn.commit(); conn.close()
    with open(path,'rb') as f: d=f.read()
    os.remove(path); return d


def _upload_db(client, table='import_items', rows=None):
    resp=client.post('/api/upload',files={'file':('upload.db',_make_import_db_bytes(table,rows),'application/octet-stream')})
    assert resp.status_code==200
    b=resp.json()
    return b.get('import_id') or b.get('id')


def test_ask_uses_selected_import_database_and_not_store_db(authed_client,monkeypatch):
    c=authed_client; iid=_upload_db(c); calls=[]
    def fake_ask(p,t,request_id=None,generation=None,database_path=None,**_):
        calls.append(database_path)
        return 'SELECT * FROM import_items','r'
    monkeypatch.setattr(main,'ask_active',fake_ask)
    gen=c.post('/api/ask',json={'prompt':'x','import_id':iid})
    assert gen.status_code==200
    g=gen.json()
    resp=c.post('/api/run',json={'sql':g['sql'],'import_id':iid,'generation_ticket':g.get('generation_ticket')})
    assert resp.status_code==200
    assert resp.json()['data']==[{'id':1,'label':'imported'}]
    assert calls and calls[0] is not None


def test_invalid_import_id_returns_error_and_does_not_call_llm(authed_client,monkeypatch):
    called=[]
    def fake_ask(*a,**k):
        called.append(True); return 'SELECT 1','ok'
    monkeypatch.setattr(main,'ask_active',fake_ask)
    for bad in ['does-not-exist','unknown123']:
        resp=authed_client.post('/api/ask',json={'prompt':'x','import_id':bad})
        assert resp.status_code in (400,404)
        assert not called


def test_malformed_import_id_rejected(authed_client,monkeypatch):
    called=[]
    def fake_ask(*a,**k):
        called.append(True); return 'SELECT 1','ok'
    monkeypatch.setattr(main,'ask_active',fake_ask)
    for bad in ['../etc/passwd','bad/../id','..','.',' ','a'*65,'has space','/etc/passwd','..\\..\\win.ini']:
        resp=authed_client.post('/api/ask',json={'prompt':'x','import_id':bad})
        assert resp.status_code in (400,404)
        assert not called


def test_path_traversal_rejected(authed_client,monkeypatch):
    called=[]
    def fake_ask(*a,**k):
        called.append(True); return 'SELECT 1','ok'
    monkeypatch.setattr(main,'ask_active',fake_ask)
    for bad in ['../../etc/passwd','..%2F..%2Fetc%2Fpasswd','%2e%2e/%2e%2e/etc/passwd']:
        resp=authed_client.post('/api/ask',json={'prompt':'x','import_id':bad})
        assert resp.status_code in (400,404)
        assert not called


def test_invalid_import_never_silently_falls_back_to_store_db(authed_client,monkeypatch):
    seen=[]
    def fake_ask(p,t,request_id=None,generation=None,database_path=None,**_):
        seen.append(database_path); return 'SELECT 1','ok'
    monkeypatch.setattr(main,'ask_active',fake_ask)
    resp=authed_client.post('/api/ask',json={'prompt':'x','import_id':'nope'})
    assert resp.status_code in (400,404)
    assert seen == []


def test_write_sql_blocked_for_uploaded_database(authed_client,monkeypatch):
    c=authed_client; iid=_upload_db(c)
    def fake_ask(p,t,request_id=None,generation=None,database_path=None,**_):
        return 'UPDATE import_items SET label=\"x\"','write'
    monkeypatch.setattr(main,'ask_active',fake_ask)
    resp=c.post('/api/ask',json={'prompt':'update','import_id':iid})
    assert resp.status_code==400


def test_ask_without_import_id_uses_store_db_behavior(authed_client,monkeypatch):
    called=[]
    def fake_ask(p,t,request_id=None,generation=None,database_path=None,**_):
        called.append(database_path); return 'SELECT * FROM products','r'
    monkeypatch.setattr(main,'ask_active',fake_ask)
    resp=authed_client.post('/api/ask',json={'prompt':'show products'})
    assert resp.status_code==200; assert called and called[0] is None