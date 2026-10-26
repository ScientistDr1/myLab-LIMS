#!/usr/bin/env python3
"""myLab LIMS platform server. Many facilities, one host: every facility gets its own private database,
its own admin and users, live sync across its devices, verified backups and a paid subscription
(daily / monthly / annual). The platform owner controls facilities from /owner and cannot read patient data.
Python 3.9+, no packages to install.   Run: python3 server.py"""
import calendar,hashlib,hmac,json,os,queue,re,secrets,sqlite3,ssl,sys,threading,time,urllib.request
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
HERE=os.path.dirname(os.path.abspath(__file__));DATA=os.environ.get('LIMS_DATA',HERE+'/data');os.makedirs(DATA+'/tenants',exist_ok=True)
PORT=int(os.environ.get('PORT','8080'));BK_HOURS=float(os.environ.get('LIMS_BACKUP_HOURS','6'))
KEEP=int(os.environ.get('LIMS_BACKUP_KEEP','30'));SESSION_H=float(os.environ.get('LIMS_SESSION_HOURS','12'))
PAYSTACK=os.environ.get('PAYSTACK_SECRET','');PUBLIC_URL=os.environ.get('LIMS_PUBLIC_URL','').rstrip('/')
# ---------- platform (host) database: facilities, plans, payments, sessions ----------
HLOCK=threading.RLock();HOST=sqlite3.connect(DATA+'/host.db',check_same_thread=False,isolation_level=None);HOST.execute('PRAGMA journal_mode=WAL')
HOST.executescript("""CREATE TABLE IF NOT EXISTS tenants(code TEXT PRIMARY KEY,name TEXT,status TEXT,created REAL,until REAL,plan TEXT,email TEXT,phone TEXT,last_login REAL);
CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,name TEXT,days INTEGER,price REAL);
CREATE TABLE IF NOT EXISTS pay(id TEXT PRIMARY KEY,code TEXT,plan TEXT,amount REAL,days INTEGER,method TEXT,ref TEXT,status TEXT,created REAL,done REAL,by TEXT,note TEXT);
CREATE TABLE IF NOT EXISTS sess(tok TEXT PRIMARY KEY,code TEXT,uid TEXT,exp REAL);
CREATE TABLE IF NOT EXISTS cfg(k TEXT PRIMARY KEY,v TEXT);
CREATE TABLE IF NOT EXISTS owner(username TEXT PRIMARY KEY,salt BLOB,hash BLOB);""")
def hall(q,a=()):
    with HLOCK: return HOST.execute(q,a).fetchall()
def hone(q,a=()):
    with HLOCK: return HOST.execute(q,a).fetchone()
def hx(q,a=()):
    with HLOCK: HOST.execute(q,a)
def cfg(k,d=''):
    r=hone('SELECT v FROM cfg WHERE k=?',(k,));return r[0] if r else d
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
ALL='admin reception cashier phleb lab mlt mls auditor'
R={'patients':(ALL,'admin reception mlt','admin'),'orders':(ALL,'admin reception phleb lab mlt mls','admin'),
'pay':('admin cashier auditor','admin cashier',''),'qcmats':('admin lab mls auditor','admin lab mls',''),
'qcruns':('admin lab mls auditor','admin lab mls',''),'tests':(ALL,'admin','admin'),
'audit':('admin lab mls auditor',ALL,''),'users':(ALL,'',''),'meta':(ALL,'admin',''),'brand':(ALL,'admin',''),'bk':(ALL,'','')}
OWN={'phleb':('sample',),'lab':('results','updated','updatedBy','validated','validatedBy','validatedAt','remarks'),'mlt':('results','updated','updatedBy','sample'),'mls':('results','updated','updatedBy','sample','validated','validatedBy','validatedAt','remarks')}  # parts of an order each role may change
APPEND=('audit','qcruns');ROLES=tuple(ALL.split())
CSP="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
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
            if role not in ('admin','lab','mls') and any(d.get(k) for k in ('validated','validatedBy','validatedAt','remarks')): raise Err(403,'Only a Medical Laboratory Scientist can validate or add final remarks')
            if (d.get('sample') or {}).get('status')=='Rejected' and not str((d.get('sample') or {}).get('rejectionReason') or '').strip(): raise Err(400,'A rejection reason is required')
            if len(json.dumps(d))>(4000000 if c=='brand' else 600000): raise Err(413,'Record too large')
            if old:
                if c in APPEND: raise Err(403,'This record cannot be edited')
                if base!=old['_v']: raise Err(409,'This record was changed by someone else.')
                if c=='pay' and role!='admin': raise Err(403,'Only an administrator can change a payment')
                if c=='orders' and role!='admin':
                    keys=(set(d)|set(old))-{'_v'}
                    bad=any(d.get(k)!=old.get(k) for k in keys if k not in OWN[role]) if role in OWN else (d.get('results')!=old.get('results') or d.get('sample')!=old.get('sample'))
                    if role in ('mlt','lab') and any(k in keys and d.get(k)!=old.get(k) for k in ('validated','validatedBy','validatedAt','remarks')): bad=True
                    if role=='mlt':
                        before=old.get('results') or {};after=d.get('results') or {}
                        if any((before.get(t) or {}).get('Remark')!=(after.get(t) or {}).get('Remark') for t in set(before)|set(after)): bad=True
                if c=='orders' and role!='admin' and bad: raise Err(403,'Your role cannot change that part of an order')
                v=old['_v']+1
            else:
                if base is not None: raise Err(409,'This record was deleted by someone else.')
                if c=='orders' and (role not in('admin','reception','mlt') or (role!='admin' and d.get('results'))): raise Err(403,'Your role cannot create orders')
                if c=='pay' and not(isinstance(d.get('amt'),(int,float)) and d['amt']>0 and not d.get('void')): raise Err(400,'Invalid payment')
                v=1
            if c=='audit': d.update(user=u['username'],role=role,ts=now())
            put(c,i,d,v);out.append(v)
        return {'rev':rev(),'vers':out}
def nxt(u,b,q):
    n=b.get('name')
    if u['role'] not in {'p':'admin reception mlt','o':'admin reception mlt','rc':'admin cashier'}.get(n,'').split(): raise Err(403,'Not allowed')
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
        if pw: setpw(d['id'],d['username'],pw)
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
def reject_sample(u,b,q):
    if u['role'] not in ('admin','mlt','mls','lab'): raise Err(403,'Only laboratory staff can reject a sample')
    oid=str(b.get('id') or '');reason=str(b.get('reason') or '').strip()
    if not reason: raise Err(400,'A rejection reason is required')
    with Tx():
        o=get('orders',oid)
        if not o: raise Err(404,'Order not found')
        o['sample']=dict(o.get('sample') or {},status='Rejected',rejectionReason=reason,rejectedBy=u['username'],rejectedAt=now())
        put('orders',oid,o,o['_v']+1);audit(u['username'],u['role'],'SAMPLE_REJECTED',str(o.get('oid',oid))+': '+reason)
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
    if not PAYSTACK or not hmac.compare_digest(hmac.new(PAYSTACK.encode(),raw,hashlib.sha512).hexdigest(),sig): raise Err(401,'bad signature')
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
    ds=time.strftime('%Y-%m-%d');ms=time.strftime('%Y-%m')
    paid=[p for p in pay if p['status']=='paid'];f=lambda fmt:sum(p['amount'] for p in paid if time.strftime(fmt,time.localtime(p['done'] or 0))==time.strftime(fmt))
    return {'tenants':rows,'payments':pay,'plans':plans(),'revenue':{'today':f('%Y-%m-%d'),'month':f('%Y-%m'),'all':sum(p['amount'] for p in paid)},
      'cfg':{k:cfg(k,d) for k,d in (('trial_days','7'),('bank',''),('support',''),('brand','myLab LIMS'))},'paystack':bool(PAYSTACK)}
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
def o_cfg(u,b,q):
    need_owner(u)
    for k in ('trial_days','bank','support','brand'):
        if k in b: setcfg(k,str(b[k])[:600])
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
def facility_code(name,email,phone,trial):
    words=re.findall(r'[A-Za-z0-9]+',name)
    prefix=''.join(w[0] for w in words).upper()[:8] or 'LAB'
    with HLOCK:
        for _ in range(1000):
            candidate='%s-%03d'%(prefix,secrets.randbelow(900)+100)
            code=candidate.lower()
            if not HOST.execute('SELECT 1 FROM tenants WHERE code=?',(code,)).fetchone() and not os.path.exists(DATA+'/tenants/'+code):
                t=time.time();HOST.execute("INSERT INTO tenants VALUES(?,?,'active',?,?,?,?,?,?)",(code,name,t,t+trial*86400,'trial',email[:100],phone[:30],t))
                return candidate,code
    raise Err(503,'Could not assign a facility code. Please try again.')
def newsession(code,uid):
    tok=secrets.token_urlsafe(32);hx('INSERT INTO sess VALUES(?,?,?,?)',(tok,code,uid,time.time()+SESSION_H*3600));return tok
def register(h,b):
    ip=h.client_address[0];REGS[ip]=[x for x in REGS.get(ip,[]) if time.time()-x<3600]
    if len(REGS[ip])>=5: raise Err(429,'Too many registrations from this address. Try again later.')
    g=lambda k:str(b.get(k,'')).strip();name=g('name');un=g('username').lower();full=g('fullname');pw=str(b.get('password',''))
    if not 3<=len(name)<=80 or not full: raise Err(400,'Enter the facility name and your full name')
    if not re.fullmatch(r'[a-z0-9._-]{3,32}',un): raise Err(400,'Username: 3-32 letters, numbers, . _ -')
    if len(pw)<8: raise Err(400,'Password must be at least 8 characters')
    trial=float(cfg('trial_days','7'));display_code,code=facility_code(name,g('email'),g('phone'),trial)
    REGS[ip].append(time.time())
    CTX.t=tenant(code);uid=secrets.token_hex(6)
    with Tx():
        u={'id':uid,'username':un,'name':full,'role':'admin','initials':'','signature':'','active':True,'created':now()}
        put('users',uid,u,1);setpw(uid,un,pw);put('meta','meta',{'id':'meta','lab':name,'pre':'','signer':full,'printMode':'blank'},1);audit(un,'admin','FACILITY_REGISTERED',name)
    return {'token':newsession(code,uid),'user':u,'facility':code,'facilityDisplay':display_code}
def login(h,b):
    fac=str(b.get('facility','')).strip().lower();un=str(b.get('username','')).strip().lower();pw=str(b.get('password',''))
    k=(h.client_address[0],fac,un);t=time.time();FAILS[k]=[x for x in FAILS.get(k,[]) if t-x<600]
    if len(FAILS[k])>=5: raise Err(429,'Too many attempts. Wait a few minutes and try again.')
    bad=Err(401,'Invalid facility code or login details.')
    if fac=='platform':
        a=hone('SELECT salt,hash FROM owner WHERE username=?',(un,))
        if not(a and hmac.compare_digest(hp(pw,a[0]),a[1])): hp(pw,b'0'*16);FAILS[k].append(t);raise bad
        return {'token':newsession('platform',un),'user':{'id':un,'username':un,'name':'Platform Owner','role':'owner'}}
    ti=hone('SELECT status FROM tenants WHERE code=?',(fac,))
    if not ti: hp(pw,b'0'*16);FAILS[k].append(t);raise bad
    if ti[0]=='suspended': raise Err(403,'This facility account is suspended. Contact the platform owner.')
    CTX.t=tenant(fac)
    with LOCK: a=con.execute('SELECT uid,salt,hash FROM auth WHERE username=?',(un,)).fetchone()
    ok=hmac.compare_digest(hp(pw,a[1]),a[2]) if a else (hp(pw,b'0'*16) and False)
    u=get('users',a[0]) if ok else None
    if not u or not u.get('active',True):
        FAILS[k].append(t)
        with Tx(): audit(un[:40] or '?','unknown','LOGIN_FAILED','Invalid credentials')
        raise bad
    with Tx(): audit(u['username'],u['role'],'LOGIN','Signed in')
    hx('UPDATE tenants SET last_login=? WHERE code=?',(time.time(),fac));FAILS.pop(k,None);u.pop('_v',None)
    return {'token':newsession(fac,u['id']),'user':u,'facility':fac}
def mkuser(h):
    q=parse_qs(urlparse(h.path).query);t=(h.headers.get('Authorization') or '')[7:] or (q.get('t') or [''])[0]
    r=hone('SELECT code,uid,exp FROM sess WHERE tok=?',(t,)) if t else None
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
        time.sleep(300)
ROUTES={('GET','/api/me'):lambda u,b,q:pub(u),('GET','/api/state'):state,('GET','/api/changes'):changes,('POST','/api/push'):push,
('POST','/api/next'):nxt,('POST','/api/users'):users,('POST','/api/password'):pwd,('POST','/api/backup/run'):runbk,
('GET','/api/sub'):sub_get,('POST','/api/sub/request'):sub_request,('POST','/api/sub/paystack'):sub_paystack,
('GET','/api/owner/overview'):o_overview,('POST','/api/owner/tenant'):o_tenant,('POST','/api/owner/record'):o_record,('POST','/api/owner/confirm'):o_confirm,
('POST','/api/owner/reject'):o_reject,('POST','/api/owner/plans'):o_plans,('POST','/api/owner/cfg'):o_cfg,('POST','/api/owner/resetadmin'):o_resetadmin,('POST','/api/sample/reject'):reject_sample}
WRITES=('/api/push','/api/next','/api/users','/api/backup/run','/api/sample/reject');OWNER_OK=('/api/me','/api/logout','/api/password')
PAGES={'/':'index.html','/index.html':'index.html','/owner':'owner.html'}
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(s,*a): pass
    def out(s,code,body,ct='application/json',extra=()):
        b=body if isinstance(body,bytes) else json.dumps(body).encode()
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
            if m=='GET' and p=='/health': return s.out(200,{'ok':True})
            n=int(s.headers.get('Content-Length') or 0)
            if n>8000000: raise Err(413,'Request too large')
            raw=s.rfile.read(n) if m=='POST' else b''
            if p=='/api/paystack/webhook': return s.out(200,paystack_hook(s,raw))
            b=json.loads(raw or b'{}') if m=='POST' else None
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
        except Exception as e: print('ERROR',p,repr(e));s.out(500,{'error':'Server error'})
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
