#!/usr/bin/env python3
"""myLab LIMS platform server. Many facilities, one host: every facility gets its own private database,
its own admin and users, live sync across its devices, verified backups and a paid subscription
(daily / monthly / annual). The platform owner controls facilities from /owner and cannot read patient data.
Python 3.9+, no packages to install.   Run: python3 server.py"""
import html as _html,calendar,hashlib,hmac,json,os,queue,re,secrets,sqlite3,ssl,sys,threading,time,urllib.request
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
HERE=os.path.dirname(os.path.abspath(__file__));DATA=os.environ.get('LIMS_DATA',HERE+'/data');os.makedirs(DATA+'/tenants',exist_ok=True)
PORT=int(os.environ.get('PORT','8080'));BK_HOURS=float(os.environ.get('LIMS_BACKUP_HOURS','6'))
KEEP=int(os.environ.get('LIMS_BACKUP_KEEP','30'));SESSION_H=float(os.environ.get('LIMS_SESSION_HOURS','12'))
PAYSTACK=os.environ.get('PAYSTACK_SECRET','');PUBLIC_URL=(os.environ.get('LIMS_PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL','')).rstrip('/')
DEMO=bool(os.environ.get('LIMS_DEMO'));PROXY=bool(os.environ.get('LIMS_BEHIND_PROXY') or os.environ.get('RENDER'))   # behind a proxy every request seems to come from one address
# ---------- platform (host) database: facilities, plans, payments, sessions ----------
HLOCK=threading.RLock();HOST=sqlite3.connect(DATA+'/host.db',check_same_thread=False,isolation_level=None);HOST.execute('PRAGMA journal_mode=WAL')
HOST.executescript("""CREATE TABLE IF NOT EXISTS tenants(code TEXT PRIMARY KEY,name TEXT,status TEXT,created REAL,until REAL,plan TEXT,email TEXT,phone TEXT,last_login REAL);
CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,name TEXT,days INTEGER,price REAL);
CREATE TABLE IF NOT EXISTS pay(id TEXT PRIMARY KEY,code TEXT,plan TEXT,amount REAL,days INTEGER,method TEXT,ref TEXT,status TEXT,created REAL,done REAL,by TEXT,note TEXT);
CREATE TABLE IF NOT EXISTS sess(tok TEXT PRIMARY KEY,code TEXT,uid TEXT,exp REAL);
CREATE TABLE IF NOT EXISTS cfg(k TEXT PRIMARY KEY,v TEXT);
CREATE TABLE IF NOT EXISTS owner(username TEXT PRIMARY KEY,salt BLOB,hash BLOB);
CREATE TABLE IF NOT EXISTS events(ts REAL,kind TEXT,code TEXT,ip TEXT,detail TEXT);
CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY,code TEXT,user TEXT,name TEXT,role TEXT,cat TEXT,subject TEXT,body TEXT,status TEXT,created REAL,updated REAL,reply TEXT);""")
def hall(q,a=()):
    with HLOCK: return HOST.execute(q,a).fetchall()
def hone(q,a=()):
    with HLOCK: return HOST.execute(q,a).fetchone()
def hx(q,a=()):
    with HLOCK: HOST.execute(q,a)
def cfg(k,d=''):
    r=hone('SELECT v FROM cfg WHERE k=?',(k,));return r[0] if r else d
START=time.time();MET={};RR={};TK={}
SUPPORT_KEYS=('support','support_phone','support_whatsapp','support_email','support_hours')
def sup_cfg(): return {k:cfg(k,'') for k in SUPPORT_KEYS}
def public_cfg(): return dict({'demo':DEMO,'brand':cfg('brand','myLab LIMS'),'privacy':bool(cfg('privacy').strip()),'terms':bool(cfg('terms').strip()),'legal_updated':cfg('legal_updated','')},**sup_cfg())
def legal_page(kind):   # the owner's text, shown as a simple public page: '# ' heading, '## ' sub-heading, '- ' bullets, blank line = new paragraph
    title={'privacy':'Privacy Policy','terms':'Terms of Service'}[kind];txt=cfg(kind,'').strip();brand=_html.escape(cfg('brand','myLab LIMS'))
    out=[];para=[];lst=[]
    def flush():
        nonlocal para,lst
        if para: out.append('<p>'+'<br>'.join(para)+'</p>');para=[]
        if lst: out.append('<ul>'+''.join('<li>%s</li>'%i for i in lst)+'</ul>');lst=[]
    for ln in txt.splitlines():
        t=ln.strip();x=_html.escape(t)
        if not t: flush()
        elif t.startswith('## '): flush();out.append('<h3>%s</h3>'%_html.escape(t[3:]))
        elif t.startswith('# '): flush();out.append('<h2>%s</h2>'%_html.escape(t[2:]))
        elif t.startswith('- '):
            if para: flush()
            lst.append(_html.escape(t[2:]))
        else:
            if lst: flush()
            para.append(x)
    flush()
    body=''.join(out) if txt else '<p>This document has not been published yet. Please check back soon.</p>'
    upd=cfg('legal_updated','');c=sup_cfg()
    contact=' &middot; '.join(_html.escape(v) for v in (c['support_email'],c['support_phone']) if v)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>%s - %s</title>'
      '<style>body{margin:0;font:16px/1.65 system-ui,Segoe UI,Roboto,sans-serif;color:#16262c;background:#f2f5f6}main{max-width:780px;margin:0 auto;padding:24px 18px 60px}'
      'article{background:#fff;border:1px solid #d9e2e5;border-radius:12px;padding:22px 24px}h1{margin:0 0 4px;font-size:26px}h2{margin:26px 0 6px;font-size:20px}h3{margin:18px 0 4px;font-size:17px}.m{color:#5d7078;font-size:14px}a{color:#1b7f92}</style></head>'
      '<body><main><p><a href="/">&larr; Back to %s</a></p><article><h1>%s</h1><div class="m">%s</div>%s</article><p class="m">%s</p></main></body></html>')%(title,brand,brand,title,('Last updated: '+_html.escape(upd)) if upd else '',body,('Questions? '+contact) if contact else '')
def evt(kind,code='',ip='',detail=''):
    try: hx('INSERT INTO events VALUES(?,?,?,?,?)',(time.time(),kind,code,ip,str(detail)[:200]))
    except Exception: pass
def cip(h):   # client address for the event log only (behind a proxy this is the address the proxy reports)
    xf=h.headers.get('X-Forwarded-For','');return (xf.split(',')[0].strip() if xf else h.client_address[0])[:45]
def meter(code):
    m=int(time.time()//60);r=MET.setdefault(m,[0,0,0]);r[0]+=1
    if code>=500: r[2]+=1
    elif code>=400: r[1]+=1
    for k in [k for k in MET if k<m-120]: del MET[k]
def setcfg(k,v): hx('INSERT INTO cfg VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',(k,str(v)))
if not hone('SELECT 1 FROM plans'):
    for i,n,d in (('daily','Daily',1),('monthly','Monthly',30),('annual','Annual',365)): hx('INSERT INTO plans VALUES(?,?,?,0)',(i,n,d))
# ---------- one private database per facility ----------
CTX=threading.local();TEN={};TLOCK=threading.Lock()
class T:
    def __init__(s,code):
        s.code=code;s.dir=DATA+'/tenants/'+code;s.bk=s.dir+'/backups';os.makedirs(s.bk,exist_ok=True)
        s.con=sqlite3.connect(s.dir+'/lims.db',check_same_thread=False,isolation_level=None);s.lock=threading.RLock();s.clients=[];s.clk=threading.Lock()
        s.con.execute('PRAGMA journal_mode=WAL')
        s.con.executescript("""CREATE TABLE IF NOT EXISTS rec(coll TEXT,id TEXT,doc TEXT,ver INTEGER,PRIMARY KEY(coll,id));
CREATE TABLE IF NOT EXISTS chg(rev INTEGER PRIMARY KEY AUTOINCREMENT,coll TEXT,id TEXT);
CREATE TABLE IF NOT EXISTS auth(uid TEXT PRIMARY KEY,username TEXT UNIQUE,salt BLOB,hash BLOB);
CREATE TABLE IF NOT EXISTS ctr(name TEXT PRIMARY KEY,n INTEGER);""")
def tenant(code):
    with TLOCK:
        if code not in TEN: TEN[code]=T(code)
        return TEN[code]
class _Con:    # the facility functions below keep using plain  con / LOCK ; these route to the current facility
    def __getattr__(s,n): return getattr(CTX.t.con,n)
class _Lock:
    def acquire(s): CTX.t.lock.acquire()
    def release(s): CTX.t.lock.release()
    def __enter__(s): s.acquire();return s
    def __exit__(s,*a): s.release()
con=_Con();LOCK=_Lock()
# collection -> (roles that read, roles that create/update, roles that delete)
ALL='admin reception cashier phleb mlt lab auditor'      # lab = Medical Laboratory Scientist (MLS), mlt = Medical Laboratory Technician
R={'patients':(ALL,'admin reception mlt lab','admin'),'orders':(ALL,'admin reception phleb mlt lab','admin'),
'pay':('admin cashier auditor','admin cashier',''),'qcmats':('admin lab mlt auditor','admin lab mlt',''),
'qcruns':('admin lab mlt auditor','admin lab mlt',''),'tests':(ALL,'admin','admin'),
'audit':('admin lab mlt auditor',ALL,''),'users':(ALL,'',''),'meta':(ALL,'admin',''),'brand':(ALL,'admin',''),'bk':(ALL,'',''),
'resetreq':('admin','','admin')}
OWN={'phleb':('sample',),'mlt':('sample','results','updated','updatedBy','rejected'),'lab':('sample','results','updated','updatedBy','rejected','validated')}  # parts of an order each role may change
def remarks(res): return {t:r.get('Remark') for t,r in (res or {}).items() if isinstance(r,dict) and r.get('Remark')}
APPEND=('audit','qcruns');ROLES=tuple(ALL.split())
CSP="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'"
class Err(Exception):
    def __init__(s,code,msg): s.code,s.msg=code,msg
now=lambda:time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
def rev():
    with LOCK: return con.execute('SELECT COALESCE(MAX(rev),0) FROM chg').fetchone()[0]
def notify():
    r=rev();t=CTX.t
    with t.clk:
        for q in t.clients: q.put(r)
class Tx:
    def __enter__(s): LOCK.acquire();con.execute('BEGIN IMMEDIATE');return s
    def __exit__(s,t,v,tb):
        try: con.execute('COMMIT' if t is None else 'ROLLBACK')
        finally: LOCK.release()
        if t is None: notify()
        return False
def get(c,i):
    with LOCK: r=con.execute('SELECT doc,ver FROM rec WHERE coll=? AND id=?',(c,i)).fetchone()
    return None if not r else dict(json.loads(r[0]),_v=r[1])
def put(c,i,d,v):
    d=dict(d);d.pop('_v',None)
    con.execute('INSERT INTO rec VALUES(?,?,?,?) ON CONFLICT(coll,id) DO UPDATE SET doc=excluded.doc,ver=excluded.ver',(c,i,json.dumps(d),v))
    con.execute('INSERT INTO chg(coll,id) VALUES(?,?)',(c,i))
def rm(c,i): con.execute('DELETE FROM rec WHERE coll=? AND id=?',(c,i));con.execute('INSERT INTO chg(coll,id) VALUES(?,?)',(c,i))
def audit(user,role,action,details=''):
    i=secrets.token_hex(6);put('audit',i,{'id':i,'ts':now(),'user':user,'role':role,'action':action,'details':str(details)[:500]},1)
def hp(pw,salt): return hashlib.scrypt(pw.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
def setpw(uid,un,pw):
    s=os.urandom(16);con.execute('INSERT INTO auth VALUES(?,?,?,?) ON CONFLICT(uid) DO UPDATE SET salt=excluded.salt,hash=excluded.hash',(uid,un,s,hp(pw,s)))
def pub(u): return {k:v for k,v in u.items() if k not in('_v','_tok')}
def readable(role): return [c for c,v in R.items() if role in v[0].split()]
def state(u,b,q):
    with LOCK:
        d={}
        for c in readable(u['role']):
            rows=con.execute("SELECT doc,ver FROM rec WHERE coll='audit' ORDER BY rowid DESC LIMIT 1000") if c=='audit' else con.execute('SELECT doc,ver FROM rec WHERE coll=?',(c,))
            d[c]=[dict(json.loads(x),_v=v) for x,v in rows]
        return {'rev':rev(),'data':d,'me':pub(u),'sub':subinfo(u['_fid'])}
def changes(u,b,q):
    since=int((q.get('since') or ['0'])[0]);rd=readable(u['role'])
    with LOCK:
        up={};dl={}
        for c,i in con.execute('SELECT DISTINCT coll,id FROM chg WHERE rev>?',(since,)).fetchall():
            if c not in rd: continue
            d=get(c,i)
            if d: up.setdefault(c,[]).append(d)
            else: dl.setdefault(c,[]).append(i)
        return {'rev':rev(),'up':up,'del':dl}
def push(u,b,q):
    role=u['role'];out=[]
    with Tx():
        for ch in (b.get('changes') or [])[:500]:
            c,i,op=ch.get('c'),str(ch.get('id') or ''),ch.get('op')
            if c not in R or not i or c in('users','bk'): raise Err(403,'Not allowed')
            if role not in R[c][2 if op=='delete' else 1].split(): raise Err(403,'Your role cannot change this data (%s)'%c)
            old=get(c,i)
            if op=='delete':
                if old: rm(c,i)
                out.append(0);continue
            d=dict(ch.get('doc') or {});base=d.pop('_v',None);d['id']=i
            if len(json.dumps(d))>(4000000 if c=='brand' else 600000): raise Err(413,'Record too large')
            if old:
                if c in APPEND: raise Err(403,'This record cannot be edited')
                if base!=old['_v']: raise Err(409,'This record was changed by someone else.')
                if c=='pay' and role!='admin': raise Err(403,'Only an administrator can change a payment')
                if c=='orders' and role!='admin':
                    keys=(set(d)|set(old))-{'_v'}
                    bad=any(d.get(k)!=old.get(k) for k in keys if k not in OWN[role]) if role in OWN else any(d.get(k)!=old.get(k) for k in ('results','sample','validated','rejected'))
                    if not bad and role in('mlt','lab') and old.get('validated') and d.get('validated') and d.get('results')!=old.get('results'): bad=True   # validated results are locked
                    if not bad and role=='mlt' and remarks(d.get('results'))!=remarks(old.get('results')): bad=True                                    # remarks: MLS only
                    if not bad and role=='lab' and d.get('validated') and d.get('validated')!=old.get('validated') and (d['validated'].get('by')!=u['username']): bad=True  # cannot sign for someone else
                    if bad: raise Err(403,'Your role cannot change that part of an order')
                v=old['_v']+1
            else:
                if base is not None: raise Err(409,'This record was deleted by someone else.')
                if c=='orders' and (role not in('admin','reception','mlt','lab') or (role!='admin' and (d.get('results') or d.get('validated') or d.get('rejected')))): raise Err(403,'Your role cannot create orders')
                if c=='pay' and not(isinstance(d.get('amt'),(int,float)) and d['amt']>0 and not d.get('void')): raise Err(400,'Invalid payment')
                v=1
            if c=='audit': d.update(user=u['username'],role=role,ts=now())
            put(c,i,d,v);out.append(v)
        return {'rev':rev(),'vers':out}
def nxt(u,b,q):
    n=b.get('name')
    if u['role'] not in {'p':'admin reception mlt lab','o':'admin reception mlt lab','rc':'admin cashier'}.get(n,'').split(): raise Err(403,'Not allowed')
    with Tx():
        con.execute('INSERT INTO ctr VALUES(?,1) ON CONFLICT(name) DO UPDATE SET n=n+1',(n,))
        return {'n':con.execute('SELECT n FROM ctr WHERE name=?',(n,)).fetchone()[0]}
def users(u,b,q):
    if u['role']!='admin': raise Err(403,'Administrators only')
    with Tx():
        i=b.get('id');old=get('users',i) if i else None
        if i and not old: raise Err(404,'No such user')
        d=dict(old) if old else {'id':secrets.token_hex(6),'created':now(),'active':True,'signature':''};d.pop('_v',None)
        for k in('name','role','initials','signature','active'):
            if k in b: d[k]=b[k]
        if not old:
            un=str(b.get('username','')).strip().lower()
            if not re.fullmatch(r'[a-z0-9._-]{3,32}',un): raise Err(400,'Username: 3-32 letters, numbers, . _ -')
            d['username']=un
        pw=b.get('password')
        if not str(d.get('name','')).strip() or d.get('role') not in ROLES: raise Err(400,'Name and a valid role are required')
        if (not old and not pw) or (pw and len(pw)<8): raise Err(400,'Password must be at least 8 characters')
        if len(str(d.get('signature','')))>400000: raise Err(400,'Signature image too large')
        if con.execute('SELECT 1 FROM auth WHERE username=? AND uid!=?',(d['username'],d['id'])).fetchone(): raise Err(400,'Username already exists')
        others=[x for x in con.execute("SELECT doc FROM rec WHERE coll='users' AND id!=?",(d['id'],)) if (lambda j:j['role']=='admin' and j.get('active',True))(json.loads(x[0]))]
        if not(d['role']=='admin' and d.get('active',True)) and not others: raise Err(400,'There must be at least one active administrator')
        v=old['_v']+1 if old else 1;put('users',d['id'],d,v)
        if pw:
            setpw(d['id'],d['username'],pw)
            if get('resetreq','rq_'+d['username']): rm('resetreq','rq_'+d['username'])
        if old and d['id']!=u['id'] and (not d.get('active',True) or pw or old['role']!=d['role']): hx('DELETE FROM sess WHERE code=? AND uid=?',(CTX.t.code,d['id']))
        audit(u['username'],'admin','USER_UPDATE' if old else 'USER_CREATE','%s (%s)%s%s'%(d['username'],d['role'],' password set' if pw else '','' if d.get('active',True) else ' disabled'))
        d['_v']=v;return {'user':d}
def pwd(u,b,q):
    new=str(b.get('new',''))
    if len(new)<8: raise Err(400,'New password must be at least 8 characters')
    with Tx():
        a=con.execute('SELECT salt,hash FROM auth WHERE uid=?',(u['id'],)).fetchone()
        if not a or not hmac.compare_digest(hp(str(b.get('old','')),a[0]),a[1]): raise Err(400,'Current password is wrong')
        setpw(u['id'],u['username'],new);hx('DELETE FROM sess WHERE code=? AND uid=? AND tok!=?',(CTX.t.code,u['id'],u['_tok']))
        audit(u['username'],u['role'],'PASSWORD_CHANGE','Own password changed')
    return {'ok':True}

# ---------- subscriptions ----------
def subinfo(code):
    r=hone('SELECT status,until,plan FROM tenants WHERE code=?',(code,))
    if not r: return None
    n=time.time();st='suspended' if r[0]=='suspended' else 'expired' if r[1]<n else 'trial' if r[2]=='trial' else 'active'
    return {'status':st,'plan':r[2],'until':r[1],'left':max(0,r[1]-n)}
def extend(code,days,plan):
    with HLOCK:
        base=max(HOST.execute('SELECT until FROM tenants WHERE code=?',(code,)).fetchone()[0],time.time())
        HOST.execute('UPDATE tenants SET until=?,plan=? WHERE code=?',(base+days*86400,plan,code))
def tlog(code,user,role,action,details):
    CTX.t=tenant(code)
    with Tx(): audit(user,role,action,details)
def settle(pid,by):   # mark a pending payment paid and extend the facility's subscription
    with HLOCK:
        p=HOST.execute("SELECT code,plan,amount,days,status FROM pay WHERE id=?",(pid,)).fetchone()
        if not p or p[4]!='pending': return False
        HOST.execute("UPDATE pay SET status='paid',done=?,by=? WHERE id=?",(time.time(),by,pid))
    extend(p[0],p[3],p[1]);tlog(p[0],by,'system','SUBSCRIPTION_PAID','%s plan, %s days, amount %s'%(p[1],p[3],p[2]));return True
def plans(): return [{'id':r[0],'name':r[1],'days':r[2],'price':r[3]} for r in hall('SELECT id,name,days,price FROM plans ORDER BY days')]
def sub_get(u,b,q):
    si=subinfo(u['_fid'])
    if u['role']!='admin': return si
    hist=[dict(zip(('id','plan','amount','days','method','ref','status','created'),r)) for r in hall('SELECT id,plan,amount,days,method,ref,status,created FROM pay WHERE code=? ORDER BY created DESC LIMIT 20',(u['_fid'],))]
    return dict(si,plans=plans(),history=hist,bank=cfg('bank'),support=cfg('support'),brand=cfg('brand','myLab LIMS'),paystack=bool(PAYSTACK))
def newpay(u,b,method):
    if u['role']!='admin': raise Err(403,'Only the facility administrator can manage the subscription')
    p=hone('SELECT id,name,days,price FROM plans WHERE id=?',(b.get('plan'),))
    if not p or not p[3]>0: raise Err(400,'This plan is not available yet. Contact the platform owner.')
    pid=secrets.token_hex(5);ref=('SUB-' if method=='transfer' else 'PS')+secrets.token_hex(5).upper()
    hx("INSERT INTO pay VALUES(?,?,?,?,?,?,?,'pending',?,NULL,NULL,?)",(pid,u['_fid'],p[0],p[3],p[2],method,ref,time.time(),str(b.get('note',''))[:200]))
    return pid,ref,p
def sub_request(u,b,q):
    pid,ref,p=newpay(u,b,'transfer');return {'id':pid,'ref':ref,'amount':p[3],'plan':p[1],'bank':cfg('bank')}
def psk(path,payload):
    req=urllib.request.Request('https://api.paystack.co'+path,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+PAYSTACK,'Content-Type':'application/json'})
    return json.loads(urllib.request.urlopen(req,timeout=20).read())
def sub_paystack(u,b,q):
    if not PAYSTACK: raise Err(400,'Online card payment is not enabled on this platform')
    pid,ref,p=newpay(u,b,'paystack');t=hone('SELECT email FROM tenants WHERE code=?',(u['_fid'],))
    try: r=psk('/transaction/initialize',{'email':t[0] or 'billing@example.com','amount':int(round(p[3]*100)),'reference':ref,'callback_url':PUBLIC_URL+'/','metadata':{'facility':u['_fid'],'plan':p[0]}})
    except Exception as e: hx("UPDATE pay SET status='failed' WHERE id=?",(pid,));raise Err(502,'Could not reach the payment provider. Try again.')
    return {'url':r['data']['authorization_url'],'ref':ref}
def paystack_hook(h,raw):
    sig=h.headers.get('x-paystack-signature','')
    if not PAYSTACK or not hmac.compare_digest(hmac.new(PAYSTACK.encode(),raw,hashlib.sha512).hexdigest(),sig): evt('BAD_WEBHOOK_SIGNATURE','',cip(h),'');raise Err(401,'bad signature')
    ev=json.loads(raw or b'{}')
    if ev.get('event')=='charge.success':
        d=ev.get('data') or {};p=hone("SELECT id,amount FROM pay WHERE ref=? AND method='paystack' AND status='pending'",(d.get('reference'),))
        if p and d.get('status')=='success' and int(d.get('amount',0))==int(round(p[1]*100)): settle(p[0],'paystack')
    return {'ok':True}
# ---------- platform owner ----------
def need_owner(u):
    if u['role']!='owner': raise Err(403,'Platform owner only')
def o_overview(u,b,q):
    need_owner(u);rows=[]
    for code,name,email,phone,created,last in hall('SELECT code,name,email,phone,created,last_login FROM tenants ORDER BY created DESC'):
        CTX.t=tenant(code)
        with LOCK:
            c=lambda k:con.execute("SELECT COUNT(*) FROM rec WHERE coll=?",(k,)).fetchone()[0]
            bk=get('bk','status');st=dict(patients=c('patients'),orders=c('orders'),users=c('users'),backup=bk and {'when':bk['when'],'ok':bk['ok']})
        rows.append(dict(code=code,name=name,email=email,phone=phone,created=created,last_login=last,**subinfo(code),**st))
    pay=[dict(zip(('id','code','plan','amount','days','method','ref','status','created','done','by','note'),r)) for r in hall('SELECT id,code,plan,amount,days,method,ref,status,created,done,by,note FROM pay ORDER BY created DESC LIMIT 200')]
    tk=[dict(zip(('id','code','user','name','role','cat','subject','body','status','created','updated','reply'),r)) for r in hall("SELECT id,code,user,name,role,cat,subject,body,status,created,updated,reply FROM tickets ORDER BY (status='open') DESC,created DESC LIMIT 100")]
    ds=time.strftime('%Y-%m-%d');ms=time.strftime('%Y-%m')
    paid=[p for p in pay if p['status']=='paid'];f=lambda fmt:sum(p['amount'] for p in paid if time.strftime(fmt,time.localtime(p['done'] or 0))==time.strftime(fmt))
    return {'tenants':rows,'tickets':tk,'payments':pay,'plans':plans(),'revenue':{'today':f('%Y-%m-%d'),'month':f('%Y-%m'),'all':sum(p['amount'] for p in paid)},
      'demo':DEMO,'cfg':{k:cfg(k,d) for k,d in (('trial_days','7'),('bank',''),('brand','myLab LIMS'),('privacy',''),('terms',''),('legal_updated',''))+tuple((k,'') for k in SUPPORT_KEYS)},'paystack':bool(PAYSTACK)}
def o_monitor(u,b,q):   # activity metadata only - never patient records
    need_owner(u);n=time.time();cur=int(n//60);mm=[v for k,v in MET.items() if k>=cur-60]
    ev=[dict(zip(('ts','kind','code','ip','detail'),r)) for r in hall('SELECT ts,kind,code,ip,detail FROM events ORDER BY ts DESC LIMIT 150')]
    sess=dict(hall('SELECT code,COUNT(*) FROM sess WHERE exp>? GROUP BY code',(n,)));fails=dict(hall("SELECT code,COUNT(*) FROM events WHERE kind='LOGIN_FAILED' AND ts>? GROUP BY code",(n-86400,)))
    fac=[]
    for code,name,last in hall('SELECT code,name,last_login FROM tenants ORDER BY last_login DESC'):
        try: size=os.path.getsize(DATA+'/tenants/'+code+'/lims.db')
        except OSError: size=0
        fac.append({'code':code,'name':name,'last_login':last,'sessions':sess.get(code,0),'failed24':fails.get(code,0),'db_kb':size//1024})
    return {'uptime':n-START,'requests':sum(v[0] for v in mm),'client_errors':sum(v[1] for v in mm),'server_errors':sum(v[2] for v in mm),'sessions':sum(sess.values()),
      'counts24':dict(hall('SELECT kind,COUNT(*) FROM events WHERE ts>? GROUP BY kind',(n-86400,))),'events':ev,'facilities':fac}
TKF=('id','cat','subject','body','status','created','updated','reply','user','name')
def sup_get(u,b,q):
    rows=hall('SELECT id,cat,subject,body,status,created,updated,reply,user,name FROM tickets WHERE code=? ORDER BY created DESC LIMIT 50',(u['_fid'],))
    return {'contact':sup_cfg(),'tickets':[dict(zip(TKF,r)) for r in rows if u['role']=='admin' or r[8]==u['username']]}
def sup_new(u,b,q):
    k=(u['_fid'],u['username']);TK[k]=[x for x in TK.get(k,[]) if time.time()-x<3600]
    if len(TK[k])>=5: raise Err(429,'You have sent several requests in the last hour. Please wait, or use the phone or WhatsApp contact.')
    subj=str(b.get('subject','')).strip();body=str(b.get('body','')).strip();cat=str(b.get('cat','Other'))[:40]
    if len(subj)<3 or len(body)<10: raise Err(400,'Please enter a subject and describe the problem in a sentence or two.')
    tid=secrets.token_hex(5);TK[k].append(time.time())
    hx("INSERT INTO tickets VALUES(?,?,?,?,?,?,?,?,'open',?,?,'')",(tid,u['_fid'],u['username'],u['name'],u['role'],cat,subj[:120],body[:3000],time.time(),time.time()))
    evt('SUPPORT_REQUEST',u['_fid'],'',subj[:60]);return {'ok':True,'id':tid}
def o_ticket(u,b,q):
    need_owner(u);tid=b.get('id');a=b.get('action');r=str(b.get('reply','')).strip()[:3000]
    if not hone('SELECT 1 FROM tickets WHERE id=?',(tid,)): raise Err(404,'No such request')
    if a in('reply','reply_close'):
        if not r: raise Err(400,'Type a reply first.')
        hx('UPDATE tickets SET reply=?,status=?,updated=? WHERE id=?',(r,'closed' if a=='reply_close' else 'open',time.time(),tid))
    elif a=='close': hx("UPDATE tickets SET status='closed',updated=? WHERE id=?",(time.time(),tid))
    elif a=='reopen': hx("UPDATE tickets SET status='open',updated=? WHERE id=?",(time.time(),tid))
    else: raise Err(400,'Unknown action')
    return {'ok':True}
def o_tenant(u,b,q):
    need_owner(u);code=b.get('code');a=b.get('action')
    if not hone('SELECT 1 FROM tenants WHERE code=?',(code,)): raise Err(404,'No such facility')
    if a=='suspend': hx("UPDATE tenants SET status='suspended' WHERE code=?",(code,));hx('DELETE FROM sess WHERE code=?',(code,))
    elif a=='activate': hx("UPDATE tenants SET status='active' WHERE code=?",(code,))
    elif a=='extend':
        d=float(b.get('days',0))
        if not 0<d<=3650: raise Err(400,'Days must be between 1 and 3650')
        extend(code,d,hone('SELECT plan FROM tenants WHERE code=?',(code,))[0])
    else: raise Err(400,'Unknown action')
    tlog(code,u['username'],'owner','OWNER_'+a.upper(),str(b.get('days','')));return {'ok':True}
def o_record(u,b,q):   # owner records a payment received outside the system (cash / bank transfer)
    need_owner(u);code=b.get('code');p=hone('SELECT id,days,price FROM plans WHERE id=?',(b.get('plan'),))
    if not p or not hone('SELECT 1 FROM tenants WHERE code=?',(code,)): raise Err(404,'Unknown facility or plan')
    amt=float(b.get('amount') or p[2])
    if amt<=0: raise Err(400,'Enter the amount received')
    pid=secrets.token_hex(5);hx("INSERT INTO pay VALUES(?,?,?,?,?,?,?,'pending',?,NULL,NULL,?)",(pid,code,p[0],amt,p[1],str(b.get('method','Bank transfer')),str(b.get('ref',''))[:60],time.time(),''))
    settle(pid,u['username']);return {'ok':True}
def o_confirm(u,b,q):
    need_owner(u)
    if not settle(b.get('id'),u['username']): raise Err(404,'Payment not found or already handled')
    return {'ok':True}
def o_reject(u,b,q):
    need_owner(u);hx("UPDATE pay SET status='rejected',done=?,by=? WHERE id=? AND status='pending'",(time.time(),u['username'],b.get('id')));return {'ok':True}
def o_plans(u,b,q):
    need_owner(u)
    for p in b.get('plans',[]):
        pr=float(p.get('price',0))
        if pr<0: raise Err(400,'Price cannot be negative')
        hx('UPDATE plans SET price=?,name=? WHERE id=?',(pr,str(p.get('name',''))[:30] or p['id'],p['id']))
    return {'ok':True}
LIM={'privacy':60000,'terms':60000,'support_phone':40,'support_whatsapp':30,'support_email':120,'support_hours':160}
def o_cfg(u,b,q):
    need_owner(u)
    for k in ('trial_days','bank','support','brand')+SUPPORT_KEYS[1:]+('privacy','terms'):
        if k in b:
            v=str(b[k])
            if k in('privacy','terms') and v.strip()!=cfg(k,'').strip(): setcfg('legal_updated',time.strftime('%d %B %Y'))
            setcfg(k,v[:LIM.get(k,600)])
    return {'ok':True}
def o_resetadmin(u,b,q):   # help a facility whose admin lost the password
    need_owner(u);code=b.get('code');un=str(b.get('username','')).lower();pw=str(b.get('password',''))
    if len(pw)<8: raise Err(400,'Password must be at least 8 characters')
    if not hone('SELECT 1 FROM tenants WHERE code=?',(code,)): raise Err(404,'No such facility')
    CTX.t=tenant(code)
    with Tx():
        a=con.execute('SELECT uid FROM auth WHERE username=?',(un,)).fetchone()
        if not a: raise Err(404,'No such user in that facility')
        setpw(a[0],un,pw);audit(u['username'],'owner','PASSWORD_RESET_BY_OWNER',un)
    hx('DELETE FROM sess WHERE code=? AND uid=?',(code,a[0]));return {'ok':True}
def ownerpw(u,b,q):
    new=str(b.get('new',''));a=hone('SELECT salt,hash FROM owner WHERE username=?',(u['username'],))
    if len(new)<8: raise Err(400,'New password must be at least 8 characters')
    if not a or not hmac.compare_digest(hp(str(b.get('old','')),a[0]),a[1]): raise Err(400,'Current password is wrong')
    s=os.urandom(16);hx('UPDATE owner SET salt=?,hash=? WHERE username=?',(s,hp(new,s),u['username']));return {'ok':True}
def setowner(un,pw):
    s=os.urandom(16);hx('INSERT INTO owner VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET salt=excluded.salt,hash=excluded.hash',(un,s,hp(pw,s)))
if len(sys.argv)>1 and sys.argv[1]=='reset-owner':
    import getpass
    pw=os.environ.get('LIMS_NEW_PASSWORD') or getpass.getpass('New platform-owner password (at least 8 characters; nothing shows while you type): ')
    if len(pw)<8: sys.exit('Password too short. Nothing was changed.')
    setowner(os.environ.get('LIMS_OWNER_USER','owner'),pw);print('Owner password changed.');sys.exit(0)
if not hone('SELECT 1 FROM owner'):
    pw=os.environ.get('LIMS_OWNER_PASSWORD') or secrets.token_urlsafe(9);un=os.environ.get('LIMS_OWNER_USER','owner');setowner(un,pw)
    print('\n  FIRST RUN - platform owner login (page /owner):  username: %s   password: %s\n  Keep it safe, then change it.\n'%(un,pw))
# ---------- sign-up, sessions, login ----------
RESERVED={'platform','owner','admin','api','www','host','demo','test'};FAILS={};REGS={}
def newsession(code,uid):
    tok=secrets.token_urlsafe(32);hx('INSERT INTO sess VALUES(?,?,?,?)',(hashlib.sha256(tok.encode()).hexdigest(),code,uid,time.time()+SESSION_H*3600));return tok   # only a hash of the token is stored
def make_code(name):
    words=re.findall(r'[A-Za-z0-9]+',name);ini=''.join(w[0] for w in words)[:5].upper()
    if len(ini)<2: ini=(re.sub(r'[^A-Za-z0-9]','',name)[:3] or 'LAB').upper()
    for _ in range(300):
        c='%s-%d'%(ini,secrets.randbelow(9000)+1000);cl=c.lower()
        if not hone('SELECT 1 FROM tenants WHERE code=?',(cl,)) and not os.path.exists(DATA+'/tenants/'+cl): return cl
    raise Err(503,'Could not create a facility code just now. Please try again.')
def register(h,b):
    ip='*' if PROXY else h.client_address[0];REGS[ip]=[x for x in REGS.get(ip,[]) if time.time()-x<3600]
    if len(REGS[ip])>=(20 if PROXY else 5): raise Err(429,'Too many registrations from this address. Try again later.')
    g=lambda k:str(b.get(k,'')).strip();name=g('name');un=g('username').lower();full=g('fullname');pw=str(b.get('password',''));em=g('email')
    if not 3<=len(name)<=80: raise Err(400,'Enter the facility name (3 to 80 characters).')
    if not full: raise Err(400,'Enter your full name.')
    if not re.fullmatch(r'[a-z0-9._-]{3,32}',un): raise Err(400,'Username: 3-32 letters, numbers, . _ -')
    if len(pw)<8: raise Err(400,'Password must be at least 8 characters.')
    if em and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',em): raise Err(400,'That email address does not look right.')
    legal=bool(cfg('privacy').strip() or cfg('terms').strip())
    if legal and not b.get('agree'): raise Err(400,'Please tick the box to accept the Privacy Policy and Terms.')
    REGS[ip].append(time.time());trial=float(cfg('trial_days','7'))
    for _ in range(20):
        code=make_code(name)
        try: hx("INSERT INTO tenants VALUES(?,?,'active',?,?,?,?,?,?)",(code,name,time.time(),time.time()+trial*86400,'trial',em[:100],g('phone')[:30],time.time()));break
        except sqlite3.IntegrityError: continue
    else: raise Err(503,'Could not create a facility code just now. Please try again.')
    CTX.t=tenant(code);uid=secrets.token_hex(6)
    with Tx():
        u={'id':uid,'username':un,'name':full,'role':'admin','initials':'','signature':'','active':True,'created':now()}
        put('users',uid,u,1);setpw(uid,un,pw);put('meta','meta',{'id':'meta','lab':name,'pre':'','signer':full,'printMode':'blank'},1);audit(un,'admin','FACILITY_REGISTERED',name+' ('+code.upper()+')'+(' | accepted Privacy Policy/Terms dated '+cfg('legal_updated','?') if legal else ''))
    evt('REGISTER',code,cip(h),name);return {'token':newsession(code,uid),'user':u,'facility':code,'facility_code':code.upper()}
def login(h,b):
    fac=str(b.get('facility','')).strip().lower();un=str(b.get('username','')).strip().lower();pw=str(b.get('password',''))
    ip=cip(h);k=('*' if PROXY else h.client_address[0],fac,un);t=time.time();FAILS[k]=[x for x in FAILS.get(k,[]) if t-x<600]
    if len(FAILS[k])>=5: evt('LOCKOUT',fac[:30],ip,un[:30]);raise Err(429,'Too many attempts. Wait a few minutes and try again.')
    def fail():
        FAILS[k].append(t);evt('LOGIN_FAILED',fac[:30],ip,un[:30]);return Err(401,'Invalid facility code or login details.')
    if fac=='platform':
        a=hone('SELECT salt,hash FROM owner WHERE username=?',(un,))
        if not(a and hmac.compare_digest(hp(pw,a[0]),a[1])): hp(pw,b'0'*16);raise fail()
        evt('OWNER_LOGIN','platform',ip,un);return {'token':newsession('platform',un),'user':{'id':un,'username':un,'name':'Platform Owner','role':'owner'}}
    ti=hone('SELECT status FROM tenants WHERE code=?',(fac,))
    if not ti: hp(pw,b'0'*16);raise fail()
    CTX.t=tenant(fac)
    with LOCK: a=con.execute('SELECT uid,salt,hash FROM auth WHERE username=?',(un,)).fetchone()
    ok=hmac.compare_digest(hp(pw,a[1]),a[2]) if a else (hp(pw,b'0'*16) and False)
    u=get('users',a[0]) if ok else None
    if not u or not u.get('active',True):
        with Tx(): audit(un[:40] or '?','unknown','LOGIN_FAILED','Invalid credentials')
        raise fail()
    if ti[0]=='suspended': evt('SUSPENDED_LOGIN',fac,ip,un);raise Err(403,'This facility account is suspended. Please contact the platform owner.')
    with Tx(): audit(u['username'],u['role'],'LOGIN','Signed in')
    hx('UPDATE tenants SET last_login=? WHERE code=?',(time.time(),fac));FAILS.pop(k,None);u.pop('_v',None)
    return {'token':newsession(fac,u['id']),'user':u,'facility':fac}
def reset_request(h,b):
    fac=str(b.get('facility','')).strip().lower();un=str(b.get('username','')).strip().lower();ip=cip(h);k='*' if PROXY else h.client_address[0]
    RR[k]=[x for x in RR.get(k,[]) if time.time()-x<600]
    if len(RR[k])>=5: raise Err(429,'Too many requests. Please wait a few minutes and try again.')
    RR[k].append(time.time())
    if un and hone('SELECT 1 FROM tenants WHERE code=?',(fac,)):
        CTX.t=tenant(fac)
        with LOCK: a=con.execute('SELECT uid FROM auth WHERE username=?',(un,)).fetchone()
        u=get('users',a[0]) if a else None
        if u and u.get('active',True) and u['role']!='admin':
            with Tx():
                rid='rq_'+un
                if not get('resetreq',rid): put('resetreq',rid,{'id':rid,'username':un,'name':u['name'],'ts':now()},1)
                audit(un,u['role'],'PASSWORD_RESET_REQUESTED','Requested from the sign-in page')
    evt('RESET_REQUEST',fac[:30],ip,un[:30]);return {'ok':True,'support':cfg('support')}
def mkuser(h):
    q=parse_qs(urlparse(h.path).query);t=(h.headers.get('Authorization') or '')[7:] or (q.get('t') or [''])[0]
    t=hashlib.sha256(t.encode()).hexdigest() if t else '';r=hone('SELECT code,uid,exp FROM sess WHERE tok=?',(t,)) if t else None
    if not r or r[2]<time.time(): return None
    code,uid=r[0],r[1]
    if code=='platform': u={'id':uid,'username':uid,'name':'Platform Owner','role':'owner'}
    else:
        ti=hone('SELECT status FROM tenants WHERE code=?',(code,))
        if not ti or ti[0]=='suspended': return None
        CTX.t=tenant(code);u=get('users',uid)
        if not u or not u.get('active',True): return None
    hx('UPDATE sess SET exp=? WHERE tok=?',(time.time()+SESSION_H*3600,t));u['_tok']=t;u['_fid']=code;return u
# ---------- backups (one set per facility) ----------
def backup(by='system'):
    t=CTX.t
    with LOCK:
        name='lims-%s.db'%time.strftime('%Y%m%d-%H%M%S');p=t.bk+'/'+name
        dst=sqlite3.connect(p);con.backup(dst);dst.close()
        v=sqlite3.connect(p);cnt=lambda c:dict(c.execute('SELECT coll,COUNT(*) FROM rec GROUP BY coll').fetchall())
        ok=v.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and cnt(v)==cnt(con);counts=cnt(v);v.close()
        sha=hashlib.sha256(open(p,'rb').read()).hexdigest()
        with Tx():
            old=get('bk','status');st={'id':'status','when':now(),'file':name,'size':os.path.getsize(p),'sha256':sha,'ok':ok,'counts':counts,'by':by}
            put('bk','status',st,old['_v']+1 if old else 1);audit(by,'admin' if by!='system' else 'system','BACKUP','%s verified=%s'%(name,ok))
        for f in sorted(x for x in os.listdir(t.bk) if x.endswith('.db'))[:-KEEP]: os.remove(t.bk+'/'+f)
    return st
def runbk(u,b,q):
    if u['role']!='admin': raise Err(403,'Administrators only')
    return backup(u['username'])
def sched():
    time.sleep(5)
    while True:
        for (code,) in hall('SELECT code FROM tenants'):
            try:
                CTX.t=tenant(code);st=get('bk','status')
                if not st or time.time()-calendar.timegm(time.strptime(st['when'],'%Y-%m-%dT%H:%M:%SZ'))>BK_HOURS*3600: backup()
            except Exception as e: print('Backup error',code,e)
        try: hx('DELETE FROM events WHERE ts<?',(time.time()-30*86400,))
        except Exception: pass
        time.sleep(300)
ROUTES={('GET','/api/me'):lambda u,b,q:pub(u),('GET','/api/state'):state,('GET','/api/changes'):changes,('POST','/api/push'):push,
('POST','/api/next'):nxt,('POST','/api/users'):users,('POST','/api/password'):pwd,('POST','/api/backup/run'):runbk,
('GET','/api/sub'):sub_get,('POST','/api/sub/request'):sub_request,('POST','/api/sub/paystack'):sub_paystack,
('GET','/api/support'):sup_get,('POST','/api/support/ticket'):sup_new,('POST','/api/owner/ticket'):o_ticket,('GET','/api/owner/overview'):o_overview,('GET','/api/owner/monitor'):o_monitor,('POST','/api/owner/tenant'):o_tenant,('POST','/api/owner/record'):o_record,('POST','/api/owner/confirm'):o_confirm,
('POST','/api/owner/reject'):o_reject,('POST','/api/owner/plans'):o_plans,('POST','/api/owner/cfg'):o_cfg,('POST','/api/owner/resetadmin'):o_resetadmin}
WRITES=('/api/push','/api/next','/api/users','/api/backup/run');OWNER_OK=('/api/me','/api/logout','/api/password')
PAGES={'/':'index.html','/index.html':'index.html','/owner':'owner.html'}
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(s,*a): pass
    def out(s,code,body,ct='application/json',extra=()):
        meter(code);b=body if isinstance(body,bytes) else json.dumps(body).encode()
        s.send_response(code);s.send_header('Content-Type',ct);s.send_header('Content-Length',str(len(b)));s.send_header('Cache-Control','no-store')
        s.send_header('X-Content-Type-Options','nosniff');s.send_header('Referrer-Policy','no-referrer');s.send_header('Content-Security-Policy',CSP)
        for k,v in extra: s.send_header(k,v)
        s.end_headers();s.wfile.write(b)
    def do_GET(s): s.route('GET')
    def do_POST(s): s.route('POST')
    def route(s,m):
        p=urlparse(s.path).path
        try:
            if m=='GET' and p in PAGES: return s.out(200,open(HERE+'/public/'+PAGES[p],'rb').read(),'text/html; charset=utf-8')
            if m=='GET' and p in ('/privacy','/terms'): return s.out(200,legal_page(p[1:]).encode(),'text/html; charset=utf-8')
            if m=='GET' and p=='/health': return s.out(200,{'ok':True})
            n=int(s.headers.get('Content-Length') or 0)
            if n>8000000: raise Err(413,'Request too large')
            raw=s.rfile.read(n) if m=='POST' else b''
            if p=='/api/paystack/webhook': return s.out(200,paystack_hook(s,raw))
            b=json.loads(raw or b'{}') if m=='POST' else None
            if p=='/api/config': return s.out(200,public_cfg())
            if p=='/api/reset-request': return s.out(200,reset_request(s,b))
            if p=='/api/login': return s.out(200,login(s,b))
            if p=='/api/register': return s.out(200,register(s,b))
            u=mkuser(s)
            if not u: raise Err(401,'Please sign in')
            owner=u['role']=='owner'
            if p.startswith('/api/owner/') and not owner: raise Err(403,'Platform owner only')
            if owner and not p.startswith('/api/owner/') and p not in OWNER_OK: raise Err(403,'The platform owner has no facility data')
            if p=='/api/events': return s.events()
            if p=='/api/logout':
                hx('DELETE FROM sess WHERE tok=?',(u['_tok'],))
                if not owner: tlog(u['_fid'],u['username'],u['role'],'LOGOUT','Signed out')
                return s.out(200,{'ok':True})
            if p=='/api/password' and owner: return s.out(200,ownerpw(u,b,None))
            if p in WRITES and not owner and subinfo(u['_fid'])['status']=='expired': raise Err(402,'Your facility subscription has expired. The data is read-only until your administrator renews it.')
            if p=='/api/backup/download':
                if u['role']!='admin': raise Err(403,'Administrators only')
                st=get('bk','status')
                if not st: raise Err(404,'No backup yet')
                with Tx(): audit(u['username'],'admin','BACKUP_DOWNLOAD',st['file'])
                return s.out(200,open(CTX.t.bk+'/'+st['file'],'rb').read(),'application/octet-stream',[('Content-Disposition','attachment; filename="%s-%s"'%(u['_fid'],st['file']))])
            fn=ROUTES.get((m,p))
            if not fn: raise Err(404,'Not found')
            s.out(200,fn(u,b,parse_qs(urlparse(s.path).query)))
        except Err as e: s.out(e.code,{'error':e.msg})
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception as e: print('ERROR',p,repr(e));evt('SERVER_ERROR','',cip(s),p);s.out(500,{'error':'Something went wrong. Please try again.'})
    def events(s):
        t=CTX.t;s.send_response(200);s.send_header('Content-Type','text/event-stream');s.send_header('Cache-Control','no-cache');s.end_headers()
        q=queue.Queue()
        with t.clk: t.clients.append(q)
        try:
            s.wfile.write(b'retry: 3000\n\ndata: {}\n\n');s.wfile.flush()
            while True:
                try: msg=('data: {"rev":%d}\n\n'%q.get(timeout=20)).encode()
                except queue.Empty: msg=b': ping\n\n'
                s.wfile.write(msg);s.wfile.flush()
        except Exception: pass
        finally:
            with t.clk: t.clients.remove(q)
            s.close_connection=True
if __name__=='__main__':
    ThreadingHTTPServer.daemon_threads=True;srv=ThreadingHTTPServer(('0.0.0.0',PORT),H)
    cert,key=os.environ.get('LIMS_CERT'),os.environ.get('LIMS_KEY')
    if cert and key: ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key);srv.socket=ctx.wrap_socket(srv.socket,server_side=True)
    threading.Thread(target=sched,daemon=True).start()
    print('myLab LIMS platform running on port %d  (%s)   facilities: /   owner console: /owner'%(PORT,'HTTPS' if cert and key else 'HTTP'));srv.serve_forever()
