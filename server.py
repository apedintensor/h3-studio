"""H3 workbench: owned media/jobs, optional password auth, durable sessions."""
from __future__ import annotations
import contextlib, hashlib, json, math, os, pathlib, re, shutil, sqlite3, subprocess
import secrets, threading, time, uuid
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError
from password_auth import initialize_auth_schema, passwords_ready, verify_password

ROOT=pathlib.Path(__file__).resolve().parent
DATA=pathlib.Path(os.environ.get('H3_STUDIO_DATA',str(ROOT/'data')))
CLOUD_STATE=pathlib.Path(os.environ.get('H3_CLOUD_STATE',str(ROOT/'cloud-state.json')))
GENERATION_ENABLED=os.environ.get('H3_GENERATION_ENABLED','1')=='1'
RELEASE=os.environ.get('H3_RELEASE','local')
COMFY=os.environ.get('H3_COMFY_URL','http://127.0.0.1:8189').rstrip('/')
if COMFY not in ('http://127.0.0.1:8189','http://127.0.0.1:8188'):
    raise RuntimeError('Use a loopback Comfy endpoint; remote access is through SSH forwarding')
for directory in ('uploads','outputs','workflows'): (DATA/directory).mkdir(parents=True,exist_ok=True)
DB=DATA/'studio.sqlite3'
STOP=threading.Event(); ACTIVE_LOCK=threading.RLock(); INFO_CACHE={}; BLOCKED=None
MODEL='MiniMax-H3-Base-BF16'
EXTENSIONS={'.png':'image','.jpg':'image','.jpeg':'image','.webp':'image',
    '.mp4':'video','.mov':'video','.wav':'audio','.mp3':'audio'}
LIMITS={'max_images':9,'max_videos':3,'max_audios':3,'max_total_files':12,
    'min_clip_duration':2,'max_clip_duration':15,'max_total_video_duration':15,
    'max_total_audio_duration':15}
USERS=frozenset(('superdan','supervan'))
LEGACY_OWNER='superdan'
SESSION_COOKIE='h3_studio_session'
SESSION_SECONDS=12*60*60
AUTH_MODE=os.environ.get('H3_AUTH_MODE','username-test')
if AUTH_MODE not in ('username-test','password'):
    raise RuntimeError('H3_AUTH_MODE must be username-test or password')
AUTHENTICATION='password' if AUTH_MODE=='password' else 'username-only-test'
LOGIN_WINDOW_SECONDS=300
LOGIN_MAX_ATTEMPTS=5
PUBLIC_ORIGIN=os.environ.get('H3_PUBLIC_ORIGIN','').rstrip('/')
PUBLIC_HOST=None
if PUBLIC_ORIGIN:
    parsed_origin=urlsplit(PUBLIC_ORIGIN)
    if (parsed_origin.scheme!='https' or not parsed_origin.hostname or parsed_origin.username
        or parsed_origin.password or parsed_origin.path or parsed_origin.query or parsed_origin.fragment
        or '*' in PUBLIC_ORIGIN or ',' in PUBLIC_ORIGIN):
        raise RuntimeError('H3_PUBLIC_ORIGIN must be one exact HTTPS origin without credentials, path or wildcards')
    PUBLIC_HOST=parsed_origin.netloc

def utc(): return datetime.now(timezone.utc).isoformat()
@contextlib.contextmanager
def db():
    connection=sqlite3.connect(DB,timeout=20)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        with connection:yield connection
    finally:connection.close()
def initialize_database():
    """Atomic, repeatable migration: existing local assets are superdan's assets."""
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,metadata TEXT NOT NULL,owner TEXT NOT NULL DEFAULT 'superdan')")
        if 'owner' not in {r[1] for r in c.execute('PRAGMA table_info(uploads)')}:
            c.execute("ALTER TABLE uploads ADD COLUMN owner TEXT NOT NULL DEFAULT 'superdan'")
        c.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,status TEXT NOT NULL,created REAL NOT NULL,record TEXT NOT NULL,idem TEXT,owner TEXT NOT NULL DEFAULT 'superdan',UNIQUE(owner,idem))")
        columns={r[1] for r in c.execute('PRAGMA table_info(jobs)')}
        old_unique=any(r[2] and [x[2] for x in c.execute('PRAGMA index_info('+r[1]+')')]==['idem']
            for r in c.execute('PRAGMA index_list(jobs)').fetchall())
        if 'owner' not in columns or old_unique:
            c.execute('ALTER TABLE jobs RENAME TO jobs_before_user_isolation')
            c.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT NOT NULL,created REAL NOT NULL,record TEXT NOT NULL,idem TEXT,owner TEXT NOT NULL DEFAULT 'superdan',UNIQUE(owner,idem))")
            owner_expression='owner' if 'owner' in columns else "'superdan'"
            c.execute('INSERT INTO jobs(id,status,created,record,idem,owner) SELECT id,status,created,record,idem,'+owner_expression+' FROM jobs_before_user_isolation')
            c.execute('DROP TABLE jobs_before_user_isolation')
        # The column is authoritative; records also carry it for the global worker.
        for jid,record,owner in c.execute('SELECT id,record,owner FROM jobs').fetchall():
            j=json.loads(record)
            if j.get('owner')!=owner:
                j['owner']=owner
                c.execute('UPDATE jobs SET record=? WHERE id=?',(json.dumps(j,ensure_ascii=False),jid))
        c.execute('CREATE INDEX IF NOT EXISTS uploads_owner_idx ON uploads(owner)')
        c.execute('CREATE INDEX IF NOT EXISTS jobs_owner_created_idx ON jobs(owner,created)')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS jobs_owner_idem_idx ON jobs(owner,idem)')
        c.execute("CREATE TABLE IF NOT EXISTS auth_sessions(token_hash TEXT PRIMARY KEY,username TEXT NOT NULL CHECK(username IN ('superdan','supervan')),created REAL NOT NULL,expires REAL NOT NULL)")
        c.execute('CREATE INDEX IF NOT EXISTS auth_sessions_expiry_idx ON auth_sessions(expires)')
        initialize_auth_schema(c)
initialize_database()

def session_hash(token):return hashlib.sha256(token.encode('ascii')).hexdigest()
def auth_ready():
    with db() as c:return AUTH_MODE!='password' or passwords_ready(c)
def authenticated_user(request):
    token=request.cookies.get(SESSION_COOKIE)
    if not token or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):return None
    digest=session_hash(token)
    with db() as c:
        row=c.execute('SELECT username,expires,auth_mode FROM auth_sessions WHERE token_hash=?',(digest,)).fetchone()
        if row and row[1]<=time.time():
            c.execute('DELETE FROM auth_sessions WHERE token_hash=?',(digest,));return None
        if row and AUTH_MODE=='password':
            password=c.execute('SELECT disabled FROM auth_passwords WHERE username=?',(row[0],)).fetchone()
            if not password or password[0] or not passwords_ready(c):return None
    return row[0] if row and row[0] in USERS and row[2]==AUTH_MODE else None

def verify_login(username,password):
    with db() as c:
        c.execute('BEGIN')
        valid=verify_password(c,username,password)
        row=c.execute('SELECT updated FROM auth_passwords WHERE username=?',(username if isinstance(username,str) else '',)).fetchone()
    return row[0] if valid and row else None

def reserve_login_attempt(request):
    # Source-based, finite fixed windows: an attacker cannot lock an account for
    # every client, and blocked attempts never extend the five-minute window.
    source=request.client.host if request.client else 'unknown'
    source_hash=hashlib.sha256(source.encode()).hexdigest();now=time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM auth_login_limits WHERE window_start<=?',(now-LOGIN_WINDOW_SECONDS,))
        row=c.execute('SELECT window_start,attempts FROM auth_login_limits WHERE source_hash=?',(source_hash,)).fetchone()
        if row and row[1]>=LOGIN_MAX_ATTEMPTS:
            retry=max(1,math.ceil(row[0]+LOGIN_WINDOW_SECONDS-now))
            raise HTTPException(429,'登录尝试过多，请稍后重试',headers={'Retry-After':str(retry)})
        c.execute('INSERT INTO auth_login_limits(source_hash,window_start,attempts) VALUES(?,?,1) ON CONFLICT(source_hash) DO UPDATE SET attempts=attempts+1',(source_hash,now))
    return source_hash

def put_job(j):
    with ACTIVE_LOCK:
        with db() as c:
            current=c.execute('SELECT status FROM jobs WHERE id=?',(j['id'],)).fetchone()
            if current and current[0]=='cancel_requested' and j['status'] in ('queued','running'):
                j['status']='cancel_requested'
            elif current and current[0]=='cancel_requested' and j['status']=='succeeded':
                j.update(status='cancelled',error='结果导出期间已取消',output_url=None,audio_output_url=None)
            c.execute('UPDATE jobs SET status=?,record=? WHERE id=?',(j['status'],json.dumps(j,ensure_ascii=False),j['id']))
def get_job(jid,owner=None):
    # owner=None is reserved for the trusted global worker and offline tests.
    with db() as c:
        row=c.execute('SELECT record FROM jobs WHERE id=?'+(' AND owner=?' if owner is not None else ''),
            (jid,owner) if owner is not None else (jid,)).fetchone()
    if not row:raise HTTPException(404,'任务不存在')
    return json.loads(row[0])
def upload_info(uid,owner=None):
    if not isinstance(uid,str) or not re.fullmatch('[0-9a-f]{32}',uid):raise HTTPException(422,'素材ID无效')
    with db() as c:
        row=c.execute('SELECT metadata FROM uploads WHERE id=?'+(' AND owner=?' if owner is not None else ''),
            (uid,owner) if owner is not None else (uid,)).fetchone()
    if not row:raise HTTPException(422,'素材不存在，请重新上传')
    return json.loads(row[0])
def public_upload(u):return {k:v for k,v in u.items() if k not in ('path','model_path')}
def probe(path):
    try:
        r=subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)],
            capture_output=True,text=True,timeout=25,check=True)
        return json.loads(r.stdout)
    except Exception:raise HTTPException(422,'文件无法解码或媒体信息无效') from None
def run_ffmpeg(args):
    try:subprocess.run(['ffmpeg','-v','error','-nostdin','-y',*args],capture_output=True,timeout=120,check=True)
    except Exception:raise HTTPException(422,'媒体转换失败，请尝试标准MP4/WAV格式') from None

def comfy_queue_idle(client):
    response=client.get(COMFY+'/queue');response.raise_for_status();queue=response.json()
    if not isinstance(queue,dict) or any(not isinstance(queue.get(key),list) for key in ('queue_running','queue_pending')):
        raise RuntimeError('无法核验GPU队列状态，未提交新任务')
    return not queue['queue_running'] and not queue['queue_pending']

def cancel_comfy_prompt(client,prompt_id):
    # Pinned ComfyUI atomically matches the running ID or dequeues this ID only.
    # False is an idempotent no-op for an already-finished/unknown ID, not failure.
    response=client.post(COMFY+'/api/jobs/'+prompt_id+'/cancel');response.raise_for_status()
    result=response.json()
    if not isinstance(result,dict) or type(result.get('cancelled')) is not bool:
        raise RuntimeError('GPU取消接口返回无效状态，请核验原任务')
    return result['cancelled']

def validate_output_audio(metadata,duration,label,flac=False):
    audio=next((s for s in metadata.get('streams',[]) if s.get('codec_type')=='audio'),None)
    try:
        actual_duration=float(audio.get('duration',metadata.get('format',{}).get('duration')))
        valid=(int(audio['sample_rate'])==32000 and audio['channels']==2
            and math.isfinite(actual_duration) and abs(actual_duration-duration)<=.1
            and (not flac or audio.get('codec_name')=='flac'))
    except (AttributeError,KeyError,TypeError,ValueError):valid=False
    if not valid:raise RuntimeError(label+'须含有效的32kHz双声道音轨，时长须与请求一致')

def saved_job_outputs(graph,record,job_id,class_type,keys,extension):
    """Read this graph's save-node outputs only; loader previews are not results."""
    prefix=job_id+('_audio' if class_type=='SaveAudioAdvanced' else '')
    found=[];seen=set()
    for node_id,out in record.get('outputs',{}).items():
        node=graph.get(str(node_id),{})
        if node.get('class_type')!=class_type or node.get('inputs',{}).get('filename_prefix')!='h3-studio/'+prefix:
            continue
        for key in keys:
            for entry in out.get(key,[]):
                filename=entry.get('filename','')
                identity=tuple(entry.get(k,'') for k in ('filename','subfolder','type'))
                if (entry.get('type')=='output' and entry.get('subfolder')=='h3-studio'
                    and isinstance(filename,str)
                    and re.fullmatch(re.escape(prefix)+r'_\d+_?'+re.escape(extension),filename)
                    and identity not in seen):
                    found.append(entry);seen.add(identity)
    return found

def validate_output_video(metadata,request):
    from comfy_workflow import native_output_spec
    spec=native_output_spec(request)
    video=next((s for s in metadata.get('streams',[]) if s.get('codec_type')=='video'),None)
    try:
        numerator,denominator=video['avg_frame_rate'].split('/')
        fps=float(numerator)/float(denominator)
        duration=float(video.get('duration',metadata.get('format',{}).get('duration')))
        valid=(video['width']==spec['width'] and video['height']==spec['height']
            and math.isclose(fps,24,abs_tol=.001) and math.isfinite(duration)
            and abs(duration-request['duration'])<=1/24+.001)
    except (AttributeError,KeyError,TypeError,ValueError,ZeroDivisionError):valid=False
    if not valid:raise RuntimeError('生成结果尺寸、24fps或时长与请求不符，不能作为成功结果')

def normalize_file(path,uid,kind):
    """Decode actual media, never trust filename or browser duration."""
    if kind=='image':
        try:
            with Image.open(path) as im:
                if getattr(im,'n_frames',1)!=1:raise ValueError('animated image')
                im.load();w,h=im.size
                if min(w,h)<256 or max(w,h)>5760 or not .4<=w/h<=2.5:raise ValueError('dimensions')
                target=DATA/'uploads'/f'{uid}.normalized.png';im.convert('RGB').save(target)
        except (ValueError,UnidentifiedImageError,OSError,Image.DecompressionBombError):
            raise HTTPException(422,'图片须为静态PNG/JPG/WEBP，边长256–5760，宽高比0.4–2.5') from None
        return {'model_path':str(target),'width':w,'height':h,'duration':None,'notes':[]}
    p=probe(path);streams=p.get('streams',[])
    try:duration=float(p['format']['duration'])
    except Exception:raise HTTPException(422,'无法确定素材时长') from None
    if not math.isfinite(duration) or not 2<=duration<=15:
        raise HTTPException(422,'视频／音频每段须为2–15秒，请先裁剪')
    audios=[s for s in streams if s['codec_type']=='audio'];videos=[s for s in streams if s['codec_type']=='video']
    if kind=='audio':
        if videos or not audios:raise HTTPException(422,'音频区只接受真正的WAV/MP3音频')
        target=DATA/'uploads'/f'{uid}.normalized.wav'
        run_ffmpeg(['-i',str(path),'-vn','-ar','32000','-ac','2',str(target)])
        return {'model_path':str(target),'duration':duration,'source_duration':duration,'has_audio':True,'notes':['转为32kHz双声道供Audio VAE编码']}
    if not videos:raise HTTPException(422,'视频文件没有有效视频轨')
    w,h=videos[0].get('width',0),videos[0].get('height',0)
    if min(w,h)<256 or max(w,h)>5760 or not .4<=w/h<=2.5:
        raise HTTPException(422,'视频边长须256–5760，宽高比0.4–2.5')
    # Extend at most 16 frames rather than silently discarding the end of a reference.
    frames=max(56,17*math.ceil((math.ceil(duration*24)-5)/17)+5)
    target=DATA/'uploads'/f'{uid}.normalized.mp4'
    args=['-i',str(path),'-vf','fps=24,tpad=stop_mode=clone:stop_duration=1',
        '-frames:v',str(frames),'-c:v','libx264','-crf','18','-pix_fmt','yuv420p']
    if audios:args+=['-af','apad','-ar','32000','-ac','2','-c:a','aac','-t',str(frames/24)]
    else:args+=['-an']
    run_ffmpeg([*args,'-movflags','+faststart',str(target)])
    actual=probe(target);actual_v=next(s for s in actual['streams'] if s['codec_type']=='video')
    if int(actual_v.get('nb_frames',0))!=frames:raise HTTPException(422,'视频帧数归一化未通过核验')
    return {'model_path':str(target),'width':w,'height':h,'duration':frames/24,
        'source_duration':duration,'fps':24,'frame_count':frames,'has_audio':bool(audios),
        'notes':[f'供模型使用：24fps / {frames}帧 / {frames/24:.3f}秒，末帧延长以对齐；原素材保留']}

def comfy_info(force=False):
    if not force and time.monotonic()-INFO_CACHE.get('time',0)<10:return INFO_CACHE.get('info')
    try:
        # The deployed node catalog is ~821 KiB over the SSH tunnel. A 3s read
        # timeout falsely marked a healthy idle GPU unavailable during testing.
        with httpx.Client(timeout=httpx.Timeout(10,connect=3),trust_env=False) as c:
            r=c.get(COMFY+'/object_info');r.raise_for_status();info=r.json()
        INFO_CACHE.update(time=time.monotonic(),info=info);return info
    except Exception:INFO_CACHE.update(time=time.monotonic(),info=None);return None

def capability():
    info=comfy_info() if GENERATION_ENABLED else None
    reason='GPU服务正在部署或SSH隧道尚未连接';available=False
    if info:
        from comfy_workflow import DIFFUSION_REF,DIFFUSION_FL,CLIP_NAME,VIDEO_VAE,AUDIO_VAE
        from comfy_workflow import REQUIRED_NODE_TYPES
        required=REQUIRED_NODE_TYPES
        missing=[n for n in required if n not in info]
        def names(node,field):
            try:return info[node]['input']['required'][field][0]
            except Exception:return []
        missing += [n for n in (DIFFUSION_REF,DIFFUSION_FL) if n not in names('UNETLoader','unet_name')]
        if CLIP_NAME not in names('CLIPLoader','clip_name'):missing.append(CLIP_NAME)
        missing += [n for n in (VIDEO_VAE,AUDIO_VAE) if n not in names('VAELoader','vae_name')]
        available=not missing and not BLOCKED
        reason=BLOCKED or ('缺少节点／权重：'+', '.join(missing) if missing else '')
    lease={}
    state=CLOUD_STATE
    if state.exists():
        s=json.loads(state.read_text());lease={k:s.get(k) for k in ('gpu','hourly_usd','created_at','ttl_hours','removal_scheduled_at','phase','status','destroyed_at','termination_verified_at')}
        expiry=lease.get('removal_scheduled_at')
        if expiry:
            parsed=datetime.fromisoformat(expiry)
            if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
            lease['removal_scheduled_at']=parsed.isoformat()
            if datetime.now(timezone.utc)>=parsed:
                available=False;reason='本轮云实例已到3小时租期，请核验／重新授权部署；不会自动续费'
        if s.get('destroyed_at') or s.get('phase')=='destroyed':
            available=False;reason='GPU已按要求停止；历史作品仍可查看和下载。重新生成需重新部署GPU。'
    if not GENERATION_ENABLED:
        available=False;reason='GPU生成已关闭；可以上传素材、设计工作流和下载已保存的作品。'
        # Disabling this website does not establish a provider's billing status.
        lease['generation_disabled']=True
    return {'backends':[{'id':'comfy-local','label':'Lium · 自部署 H3 Base BF16','available':available,
        'reason':reason,'modes':['ref','fl'],'limits':LIMITS,'models':[{'id':MODEL,
        'label':'H3 Base · BF16 · 无Turbo','min_duration':4,'max_duration':15,'resolutions':['480P','576P','768P','custom']}]}],
        'default_backend':'comfy-local','features':{'cancel':True,'controls':{'seed':True,'steps':True,'generate_audio':True}},
        'controls':control_metadata(),'expert_url':None if PUBLIC_ORIGIN or not GENERATION_ENABLED else 'http://127.0.0.1:8189/',
        'lease':lease,'scope':'自部署原生尺寸控制（768p默认）；不包含未开源的官方Context-IR及2K再生成',
        'native_timing':'原生输出为17n+5帧；导出按请求秒数裁尾，参考视频对齐时延长末帧，不裁原片。'}

def control_metadata():
    """Pinned engine controls: describes offline graph support, not GPU quality validation."""
    from comfy_workflow import controls_metadata
    return controls_metadata()

def experimental_warnings(request):
    warnings=[]
    if request['duration']<5:warnings.append('4秒低于官方常用训练时长；请对照控制验收记录，未测组合仍是实验。')
    if request['resolution']!='768P':warnings.append('低分辨率／自定义尺寸是原生节点控制；请对照控制验收记录，未测组合仍是实验，不代表1080p或2K再生成。')
    changed=[k for k,default in (('sampler_name','res_multistep'),('scheduler','auto'),('denoise',1.0),
        ('ref_image_size','max'),('video_decode','normal'),('audio_decode','normal'),('encoder_device','default'))
        if request.get(k,default)!=default]
    if request.get('shift_video') is not None or request.get('shift_audio') is not None:changed.append('sigma shift')
    if request.get('guides'):changed.append('时间锚点')
    if any(v is False for v in request.get('video_audio',{}).values()):changed.append('视频参考静音')
    if changed:warnings.append('新增／非默认控制请对照控制验收记录；未测组合仍是实验：'+', '.join(changed))
    if request.get('steps',20)!=20:warnings.append('步数请对照控制验收记录；Base常用20–25步，未测组合仍是实验，更多步数不保证更好。')
    if request.get('denoise',1)<1:warnings.append('低denoise用于带约束潜变量的实验；纯文本空潜变量可能质量下降。')
    return warnings

def seed_value(value):
    if value is None:return int.from_bytes(os.urandom(4),'big')
    if isinstance(value,str):
        if not re.fullmatch(r'[0-9]{1,20}',value):raise HTTPException(422,'seed字符串须为0–2^64-1十进制整数')
        value=int(value)
    if type(value) is not int or not 0<=value<=2**64-1:raise HTTPException(422,'seed须为0–2^64-1整数或十进制字符串')
    # JSON numbers above this boundary lose precision in the browser.
    return str(value) if value>2**53-1 else value

def control_properties():
    """OpenAPI uses the pinned node choices without probing GPU services."""
    info=json.loads((ROOT/'comfy-object-info.json').read_text(encoding='utf-8'))
    def choices(node,key):
        value=info[node]['input']['required'][key]
        return value[1]['options'] if value[0]=='COMBO' else value[0]
    integer=lambda low,high,default,**extra:{'type':'integer','minimum':low,'maximum':high,'default':default,**extra}
    floating=lambda low,high,default:{'type':'number','minimum':low,'maximum':high,'default':default}
    return {
        'width':integer(256,1536,1344,multipleOf=32,description='仅custom尺寸使用；像素面积最多768×1344且宽高比0.4–2.5。'),
        'height':integer(256,1536,768,multipleOf=32,description='仅custom尺寸使用，32倍数；保持原生生成像素，不是导出缩放。'),
        'sampler_name':{'type':'string','enum':choices('KSamplerSelect','sampler_name'),'default':'res_multistep'},
        'scheduler':{'type':'string','enum':['auto',*choices('BasicScheduler','scheduler')],'default':'auto',
            'description':'auto保留原图：ref使用beta，fl使用simple。其他值是实验控制，未做GPU质量比较。'},
        'denoise':floating(.01,1,1),
        'ref_image_size':{'type':'string','enum':['match','max'],'default':'max',
            'description':'ref模式：max保留参考身份细节，match降低参考像素面积及开销。'},
        'video_audio':{'type':'object','default':{},'additionalProperties':{'type':'boolean'},
            'description':'参考视频上传ID→是否同时使用其声音；未列出的ID默认true。不能引用本请求之外的video ID。'},
        'shift_video':{'anyOf':[floating(.01,100,12),{'type':'null'}],'default':None,
            'description':'null保持原模型采样；显式设置才添加MiniMaxH3SigmaShift节点。'},
        'shift_audio':{'anyOf':[floating(.01,100,3),{'type':'null'}],'default':None},
        'guides':{'type':'array','maxItems':8,'default':[],'description':'时间锚点：图片或视频定位到指定时间，可选视频声音；独立音频可作为声音锚点。超出导出视频的尾部会被拒绝，不自动裁素材。',
            'items':{'type':'object','required':['media_id','time_seconds'],'additionalProperties':False,
                'properties':{'media_id':{'type':'string','pattern':'^[0-9a-f]{32}$'},
                    'time_seconds':{'type':'number','minimum':0,'exclusiveMaximum':15},
                    'use_audio':{'type':'boolean','default':False}}}},
        'video_decode':{'type':'string','enum':['normal','tiled'],'default':'normal'},
        'audio_decode':{'type':'string','enum':['normal'],'default':'normal',
            'description':'当前H3音频VAE分块解码已实测不兼容；仅支持完整解码。'},
        'video_tile_size':integer(64,4096,512,multipleOf=32),
        'video_overlap':integer(0,4096,64,multipleOf=32),
        'video_temporal_size':integer(8,4096,64,multipleOf=4),
        'video_temporal_overlap':integer(4,4096,8,multipleOf=4),
        'audio_tile_size':integer(32,8192,512,multipleOf=8),
        'audio_overlap':integer(0,1024,64,multipleOf=8),
        'encoder_device':{'type':'string','enum':['default','cpu'],'default':'default',
            'description':'文本／多模态编码器执行设备；不改模型文件精度。cpu可能明显减慢。'},
        'export_crf':integer(0,51,18,description='MP4 H.264压缩：越低失真越小且文件越大；不会提高模型原生清晰度。')}

def validate_job(raw,owner=None):
    if not isinstance(raw,dict):raise HTTPException(422,'请求必须是JSON对象')
    if raw.get('backend')!='comfy-local' or raw.get('model')!=MODEL:raise HTTPException(422,'后端或model ID不匹配')
    mode=raw.get('mode');prompt=raw.get('prompt')
    if mode not in ('ref','fl'):raise HTTPException(422,'模式无效')
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>12000:raise HTTPException(422,'请填写1–12000字符的prompt')
    if type(raw.get('duration')) is not int or not 4<=raw['duration']<=15:raise HTTPException(422,'生成长度须为4–15整数秒')
    if type(raw.get('generate_audio',True)) is not bool:raise HTTPException(422,'generate_audio须为boolean')
    if raw.get('audio_decode')=='tiled':raise HTTPException(422,'当前H3音频VAE分块解码已实测不兼容，请使用完整解码')
    i=raw.get('inputs',{})
    if not isinstance(i,dict):raise HTTPException(422,'inputs须为对象')
    if set(i)-{'images','videos','audios','first_frame','last_frame'}:raise HTTPException(422,'inputs包含不支持的字段')
    i={k:i.get(k,[] if k in ('images','videos','audios') else None) for k in ('images','videos','audios','first_frame','last_frame')}
    for k in ('images','videos','audios'):
        if not isinstance(i[k],list):raise HTTPException(422,k+'须为素材ID列表')
    if mode=='fl' and any(i[k] for k in ('images','videos','audios')):raise HTTPException(422,'首尾帧模式不能混合全能参考素材')
    if mode=='ref' and (i['first_frame'] or i['last_frame']):raise HTTPException(422,'全能参考模式不能混入首尾帧约束')
    if len(i['images'])>9 or len(i['videos'])>3 or len(i['audios'])>3 or sum(len(i[k]) for k in ('images','videos','audios'))>12:
        raise HTTPException(422,'最多9图、3视频、3音频，三类合计最多12文件')
    all_ids=[*i['images'],*i['videos'],*i['audios'],*[v for v in (i['first_frame'],i['last_frame']) if v]]
    if any(not isinstance(uid,str) for uid in all_ids):raise HTTPException(422,'素材ID必须是字符串')
    if len(all_ids)!=len(set(all_ids)):raise HTTPException(422,'同一参考素材不能重复添加；时间锚点可以复用参考素材')
    guides=raw.get('guides',[])
    if not isinstance(guides,list) or len(guides)>8:raise HTTPException(422,'guides须为最多8个时间锚点的列表')
    for guide in guides:
        if not isinstance(guide,dict) or set(guide)-{'media_id','time_seconds','use_audio'}:
            raise HTTPException(422,'每个锚点须为media_id、time_seconds及可选use_audio对象')
        uid=guide.get('media_id');stamp=guide.get('time_seconds')
        if not isinstance(uid,str):raise HTTPException(422,'锚点media_id须为素材ID')
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<=stamp<raw['duration']:
            raise HTTPException(422,'锚点时间须在生成视频范围内')
        if type(guide.get('use_audio',False)) is not bool:raise HTTPException(422,'锚点use_audio须为boolean')
        if uid not in all_ids:all_ids.append(uid)
    uploads={uid:upload_info(uid,owner=owner) for uid in all_ids}
    for k,kind in (('images','image'),('videos','video'),('audios','audio')):
        if any(uploads[uid]['kind']!=kind for uid in i[k]):raise HTTPException(422,'素材类型与输入区域不匹配')
    for k in ('first_frame','last_frame'):
        if i[k] and uploads[i[k]]['kind']!='image':raise HTTPException(422,'首尾帧必须是图片')
    for k in ('videos','audios'):
        if sum(uploads[u].get('source_duration',uploads[u]['duration']) for u in i[k])>15:
            raise HTTPException(422,('参考视频' if k=='videos' else '参考音频')+'合计不得超过15秒')
    from comfy_workflow import native_output_spec,validate_controls,build_workflow
    request={**raw,'seed':seed_value(raw.get('seed')),'generate_audio':raw.get('generate_audio',True),'inputs':i}
    try:
        spec=native_output_spec(request)
        controls=validate_controls(request,uploads,spec)
        if controls is not None:request.update(controls)
        request['seed']=seed_value(request['seed'])
        # Native nodes may crop reference/guide tails: reject unrequested cropping.
        limit=spec.get('actual_duration',spec.get('frames',124)/24)
        if any(uploads[u]['duration']>limit+.001 for u in i['videos']):
            raise ValueError('参考视频长于生成视频。请提高生成时长，避免模型自动裁掉参考尾部')
        for guide in guides:
            meta=uploads[guide['media_id']];frame=round(guide['time_seconds']*24)
            if frame>=request['duration']*24:raise ValueError('锚点取整后超过最终导出时长')
            if meta['kind'] in ('video','audio') and (frame/24+meta['duration']>request['duration']+.001):
                raise ValueError('锚点素材超出最终导出时长；请调整时间或先明确裁剪素材')
        build_workflow(request,uploads,{uid:pathlib.Path(u['model_path']).name for uid,u in uploads.items()})
    except (ValueError,TypeError) as e:raise HTTPException(422,str(e)) from None
    return request,uploads

@contextlib.asynccontextmanager
async def lifespan(app):
    STOP.clear()
    if not GENERATION_ENABLED:
        # CPU-only releases must not contact GPU, recover/alter jobs, or start its worker.
        yield
        STOP.set()
        return
    # Recovery is honest: do not automatically resubmit a GPU request after a process restart.
    with db() as c:
        rows=c.execute("SELECT record FROM jobs WHERE status IN ('running','cancel_requested')").fetchall()
    for row in rows:
        j=json.loads(row[0]);j.update(status='failed',error='本地服务重启，原GPU任务状态需核验；未自动重投');put_job(j)
    t=threading.Thread(target=worker,daemon=True,name='h3-single-worker');t.start()
    yield
    STOP.set();t.join(timeout=3)

app=FastAPI(title='MiniMax H3 Studio API',version='1.2.1',lifespan=lifespan)

@app.get('/healthz',include_in_schema=False)
def healthz():
    # No provider request, session creation, file paths or user data in health checks.
    try:
        with db() as c:c.execute('SELECT 1 FROM jobs LIMIT 1').fetchall()
    except sqlite3.Error:
        return JSONResponse({'status':'unavailable'},status_code=503)
    ready=auth_ready()
    result={'status':'ok' if ready else 'unavailable','release':RELEASE,'generation_enabled':GENERATION_ENABLED,
        'authentication':AUTHENTICATION,'auth_ready':ready}
    return result if ready else JSONResponse(result,status_code=503)

@app.middleware('http')
async def local_boundary(request,call_next):
    # Username-only sessions are expressly for the two-user test, not identity proof.
    full_host=request.headers.get('host','')
    host=full_host.split(':')[0]
    if host not in ('127.0.0.1','localhost','testserver') and full_host!=PUBLIC_HOST:
        return JSONResponse({'detail':'访问域名未获允许'},status_code=403)
    origin=request.headers.get('origin')
    if origin and origin not in ('http://127.0.0.1:8844','http://localhost:8844','http://testserver',PUBLIC_ORIGIN):
        return JSONResponse({'detail':'跨站请求被拒绝'},status_code=403)
    path=request.url.path
    is_api=path=='/api' or path.startswith('/api/')
    is_native_starter=path=='/h3-ref-bf16-workflow.json'
    if (is_api and path not in ('/api/auth/login','/api/auth/logout','/api/auth/config')) or is_native_starter:
        user=authenticated_user(request)
        if not user:
            return JSONResponse({'detail':'请先登录'},status_code=401,headers={'Cache-Control':'private, no-store'})
        request.state.username=user
        if is_native_starter and user!='superdan':
            return JSONResponse({'detail':'文件不存在'},status_code=404,headers={'Cache-Control':'private, no-store'})
    r=await call_next(request)
    r.headers['X-Content-Type-Options']='nosniff'
    if is_api or is_native_starter:r.headers['Cache-Control']='private, no-store';r.headers['Vary']='Cookie'
    return r

@app.get('/api/auth/config',summary='登录方式与初始化状态')
def auth_config():return {'authentication':AUTHENTICATION,'auth_ready':auth_ready()}

@app.post('/api/auth/login',summary='登录并建立会话',
    openapi_extra={'requestBody':{'required':True,'content':{'application/json':{'schema':{
        'type':'object','required':['username','password'] if AUTH_MODE=='password' else ['username'],'additionalProperties':False,
        'properties':{'username':{'type':'string','maxLength':32},
            **({'password':{'type':'string','format':'password','writeOnly':True,'description':'最多72个UTF-8字节'}} if AUTH_MODE=='password' else {})}}}}}})
async def login(request:Request):
    source_hash=None;verified_revision=None
    if AUTH_MODE=='password':
        if not auth_ready():raise HTTPException(503,'登录服务尚未完成初始化，请联系管理员')
        source_hash=reserve_login_attempt(request)
        try:
            body=bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>2048:raise ValueError('body too large')
            raw=json.loads(body)
        except (ValueError,UnicodeError):raw=None
        valid=isinstance(raw,dict) and set(raw)=={'username','password'}
        username=raw.get('username') if valid else None
        password=raw.get('password') if valid else None
        verified_revision=await run_in_threadpool(verify_login,username,password)
        if verified_revision is None:
            raise HTTPException(401,'用户名或密码错误')
    else:
        try:raw=await request.json()
        except Exception:raise HTTPException(422,'无法解析JSON') from None
        if (not isinstance(raw,dict) or set(raw)!={'username'}
            or not isinstance(raw['username'],str) or raw['username'] not in USERS):
            raise HTTPException(422,'仅允许测试用户 superdan 或 supervan')
    token=secrets.token_urlsafe(32);now=time.time()
    old_token=request.cookies.get(SESSION_COOKIE)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if AUTH_MODE=='password':
            current=c.execute('SELECT updated,disabled FROM auth_passwords WHERE username=?',(raw['username'],)).fetchone()
            if not current or current[0]!=verified_revision or current[1] or not passwords_ready(c):
                raise HTTPException(401,'用户名或密码错误')
        c.execute('DELETE FROM auth_sessions WHERE expires<=?',(now,))
        if old_token and re.fullmatch(r'[A-Za-z0-9_-]{43}',old_token):
            c.execute('DELETE FROM auth_sessions WHERE token_hash=?',(session_hash(old_token),))
        if source_hash:c.execute('DELETE FROM auth_login_limits WHERE source_hash=?',(source_hash,))
        c.execute('INSERT INTO auth_sessions(token_hash,username,created,expires,auth_mode) VALUES(?,?,?,?,?)',
            (session_hash(token),raw['username'],now,now+SESSION_SECONDS,AUTH_MODE))
    response=JSONResponse({'username':raw['username'],'authentication':AUTHENTICATION})
    response.set_cookie(SESSION_COOKIE,token,max_age=SESSION_SECONDS,path='/',httponly=True,
        secure=request.url.scheme=='https',samesite='lax')
    return response

@app.get('/api/auth/me',summary='当前登录用户')
def auth_me(request:Request):return {'username':request.state.username,'authentication':AUTHENTICATION}

@app.post('/api/auth/logout',summary='退出测试用户')
def logout(request:Request):
    token=request.cookies.get(SESSION_COOKIE)
    if token and re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
        with db() as c:c.execute('DELETE FROM auth_sessions WHERE token_hash=?',(session_hash(token),))
    response=JSONResponse({'logged_out':True})
    response.delete_cookie(SESSION_COOKIE,path='/',httponly=True,secure=request.url.scheme=='https',samesite='lax')
    return response

@app.get('/api/capabilities',summary='查询后端能力与可用状态')
def capabilities(request:Request):
    result=capability()
    return {**result,'expert_url':result.get('expert_url') if request.state.username=='superdan' else None,
        'authentication':AUTHENTICATION}

@app.post('/api/uploads',summary='上传并验证参考素材',
    description='图片最多30 MiB、视频50 MiB、音频15 MiB；视频和音频每段须2–15秒。返回的id用于生成请求。')
async def upload(request:Request,file:UploadFile=File(...)):
    name=pathlib.Path(file.filename or '').name;ext=pathlib.Path(name).suffix.lower();kind=EXTENSIONS.get(ext)
    if not kind:raise HTTPException(422,'支持PNG/JPG/WEBP、MP4/MOV、WAV/MP3')
    uid=uuid.uuid4().hex;path=DATA/'uploads'/(uid+ext);size=0
    max_size={'image':30,'video':50,'audio':15}[kind]*1024*1024
    try:
        with path.open('wb') as f:
            while chunk:=await file.read(1024*1024):
                size+=len(chunk)
                if size>max_size:raise HTTPException(413,f'{kind}单文件大小超过限制')
                f.write(chunk)
        if not size:raise HTTPException(422,'空文件')
        meta=normalize_file(path,uid,kind)
        u={'id':uid,'name':name,'kind':kind,'size':size,'path':str(path),
            'preview_url':f'/api/uploads/{uid}/content','created_at':utc(),**meta}
        with db() as c:c.execute('INSERT INTO uploads(id,metadata,owner) VALUES(?,?,?)',
            (uid,json.dumps(u,ensure_ascii=False),request.state.username))
        return public_upload(u)
    except Exception:
        path.unlink(missing_ok=True)
        for f in (DATA/'uploads').glob(uid+'.normalized.*'):f.unlink(missing_ok=True)
        raise
    finally:await file.close()

@app.get('/api/uploads/{uid}/content')
def media_content(uid:str,request:Request):return FileResponse(upload_info(uid,owner=request.state.username)['path'])

@app.get('/api/uploads/{uid}')
def upload_metadata(uid:str,request:Request):return public_upload(upload_info(uid,owner=request.state.username))

@app.post('/api/workflow-preview',summary='离线校验控制项并预览原生工作流',
    description='只读取已上传素材的元数据，校验参数并生成工作流；不检查GPU健康、不入队、不推理、不租赁。',
    openapi_extra={'requestBody':{'required':True,'content':{'application/json':{'schema':{
        'type':'object','required':['backend','model','mode','prompt','duration','resolution','aspect_ratio'],
        'properties':{'backend':{'type':'string','enum':['comfy-local']},'model':{'type':'string','enum':[MODEL]},
            'mode':{'type':'string','enum':['ref','fl']},'prompt':{'type':'string','minLength':1,'maxLength':12000},
            'duration':{'type':'integer','minimum':4,'maximum':15},
            'resolution':{'type':'string','enum':['480P','576P','768P','custom']},
            'aspect_ratio':{'type':'string','enum':['16:9','9:16','1:1','4:3','3:4','21:9']},
            'generate_audio':{'type':'boolean','default':True},'steps':{'type':'integer','minimum':1,'maximum':100,'default':20},
            'seed':{'anyOf':[{'type':'integer','minimum':0,'maximum':2**64-1},{'type':'string','pattern':'^[0-9]{1,20}$'},{'type':'null'}]},
            'inputs':{'type':'object'},**control_properties()}}}}}})
async def workflow_preview(request:Request):
    try:raw=await request.json()
    except Exception:raise HTTPException(422,'无法解析JSON') from None
    args,uploads=validate_job(raw,owner=request.state.username)
    from comfy_workflow import build_workflow,native_output_spec
    graph=build_workflow(args,uploads,{uid:pathlib.Path(u['model_path']).name for uid,u in uploads.items()})
    return {'request':args,'native_spec':native_output_spec(args),'graph':graph,
        'warnings':experimental_warnings(args),'gpu_submitted':False,
        'verification':'离线节点和参数校验；不表示新控制已完成GPU推理或质量验收'}

@app.post('/api/jobs',status_code=202,summary='提交H3音视频生成任务',
    description='异步入队并返回任务记录。fl模式可不提供图片进行纯文本生成，或提供首帧、尾帧、两帧；ref模式至少需要一个参考素材。素材ID须先由上传接口取得。',
    responses={202:{'description':'返回任务对象。幂等重放返回已有记录；示例仅列出主要字段，省略的output_url/error在排队任务中为null，ID为静态占位值。',
        'content':{'application/json':{
            'schema':{'type':'object','required':['id','status','seed','request','output_url','error','elapsed_seconds'],
                'properties':{
                    'id':{'type':'string','pattern':'^[0-9a-f]{32}$'},
                    'status':{'type':'string','enum':['queued','running','cancel_requested','cancelled','succeeded','failed']},
                    'seed':{'anyOf':[{'type':'integer','minimum':0,'maximum':2**64-1},{'type':'string','pattern':'^[0-9]{1,20}$'}]},
                    'request':{'type':'object','description':'经过校验的生成参数，包含实际seed及默认值。'},
                    'output_url':{'anyOf':[{'type':'string'},{'type':'null'}],
                        'description':'成功后可下载MP4的本地相对URL；尚无结果时为null。'},
                    'error':{'anyOf':[{'type':'string'},{'type':'null'}],'description':'错误或取消说明；正常任务为null。'},
                    'elapsed_seconds':{'type':'number','minimum':0}}},
            'example':{'id':'00000000000000000000000000000000','status':'queued','seed':42,
                'request':{'backend':'comfy-local','model':MODEL,'mode':'fl',
                    'prompt':'A quiet cinematic sunrise over mountains with soft wind and birdsong.',
                    'duration':5,'resolution':'768P','aspect_ratio':'16:9','generate_audio':True,
                    'steps':20,'seed':42,'inputs':{'images':[],'videos':[],'audios':[],
                        'first_frame':None,'last_frame':None}},
                'output_url':None,'error':None,'elapsed_seconds':0}}}}},
    openapi_extra={
        'parameters':[{'name':'Idempotency-Key','in':'header','required':False,
            'description':'可选幂等键；相同键和相同请求返回已有任务，相同键配不同请求返回409。每次新的生成请使用新键。',
            'schema':{'type':'string','minLength':8,'maxLength':128,'pattern':'^[a-zA-Z0-9_-]{8,128}$'}}],
        'requestBody':{'required':True,'content':{'application/json':{
            'schema':{'type':'object',
                'required':['backend','model','mode','prompt','duration','resolution','aspect_ratio'],
                'properties':{
                    'backend':{'type':'string','enum':['comfy-local'],'default':'comfy-local'},
                    'model':{'type':'string','enum':[MODEL],'default':MODEL},
                    'mode':{'type':'string','enum':['ref','fl'],'description':'ref为全能参考，fl为纯文本／首尾帧。'},
                    'prompt':{'type':'string','minLength':1,'maxLength':12000,'description':'生成描述，不能只含空白。'},
                    'duration':{'type':'integer','minimum':4,'maximum':15,'default':5,'description':'输出时长，整数秒。'},
                    'resolution':{'type':'string','enum':['480P','576P','768P','custom'],'default':'768P'},
                    'aspect_ratio':{'type':'string','enum':['16:9','9:16','1:1','4:3','3:4','21:9'],'default':'16:9'},
                    'generate_audio':{'type':'boolean','default':True,'description':'是否导出生成音轨及独立FLAC；关闭时音频参考仍可参与条件。'},
                    'steps':{'type':'integer','minimum':1,'maximum':100,'default':20},
                    'seed':{'anyOf':[{'type':'integer','minimum':0,'maximum':2**64-1},{'type':'string','pattern':'^[0-9]{1,20}$'},{'type':'null'}],
                        'default':None,'description':'0–2^64-1；超过2^53-1须使用十进制字符串，避免浏览器损失精度。省略/null时随机。'},
                    **control_properties(),
                    'inputs':{'type':'object','default':{},
                        'description':'ref模式三类参考合计最多12文件，不能使用first_frame/last_frame。fl模式三类参考列表须为空。所有素材ID须有效且不能重复。',
                        'properties':{
                            'images':{'type':'array','maxItems':9,'uniqueItems':True,'default':[],
                                'items':{'type':'string','pattern':'^[0-9a-f]{32}$'},'description':'参考图片上传ID，按列表顺序编号。'},
                            'videos':{'type':'array','maxItems':3,'uniqueItems':True,'default':[],
                                'items':{'type':'string','pattern':'^[0-9a-f]{32}$'},
                                'description':'参考视频上传ID；每段原素材2–15秒，视频原时长合计最多15秒，参考视频不能长于生成视频。'},
                            'audios':{'type':'array','maxItems':3,'uniqueItems':True,'default':[],
                                'items':{'type':'string','pattern':'^[0-9a-f]{32}$'},
                                'description':'独立音频上传ID；每段2–15秒，音频合计最多15秒，与视频总时长分别计算。'},
                            'first_frame':{'anyOf':[{'type':'string','pattern':'^[0-9a-f]{32}$'},{'type':'null'}],
                                'default':None,'description':'首帧图片上传ID，仅fl模式。'},
                            'last_frame':{'anyOf':[{'type':'string','pattern':'^[0-9a-f]{32}$'},{'type':'null'}],
                                'default':None,'description':'尾帧图片上传ID，仅fl模式。'}}}}},
            'examples':{'text_only':{'summary':'FL模式：纯文本生成5秒音视频',
                'value':{'backend':'comfy-local','model':MODEL,'mode':'fl',
                    'prompt':'A quiet cinematic sunrise over mountains with soft wind and birdsong.',
                    'duration':5,'resolution':'768P','aspect_ratio':'16:9','generate_audio':True,
                    'steps':20,'seed':42,'inputs':{'images':[],'videos':[],'audios':[],
                        'first_frame':None,'last_frame':None}}}}}}}})
async def create_job(request:Request):
    try:raw=await request.json()
    except Exception:raise HTTPException(422,'无法解析JSON') from None
    owner=request.state.username
    args,uploads=validate_job(raw,owner=owner)
    idem=request.headers.get('idempotency-key')
    fingerprint=hashlib.sha256(json.dumps(raw,sort_keys=True).encode()).hexdigest()
    if idem:
        if not re.fullmatch('[a-zA-Z0-9_-]{8,128}',idem):raise HTTPException(422,'Idempotency-Key格式无效')
        with db() as c:row=c.execute('SELECT record FROM jobs WHERE owner=? AND idem=?',(owner,idem)).fetchone()
        if row:
            j=json.loads(row[0])
            if j.get('request_fingerprint')!=fingerprint:raise HTTPException(409,'同一幂等键对应不同请求')
            return j
    cap=capability()['backends'][0]
    if not cap['available']:raise HTTPException(503,cap['reason'])
    jid=uuid.uuid4().hex
    j={'id':jid,'owner':owner,'status':'queued','progress':None,'created_at':utc(),'backend':args['backend'],
        'model':args['model'],'prompt':args['prompt'],'request':args,'request_fingerprint':fingerprint,'warnings':experimental_warnings(args),
        'seed':args['seed'],'output_url':None,'error':None,'elapsed_seconds':0}
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','cancel_requested')").fetchone()[0]>=8:
            raise HTTPException(429,'队列已满（最多8任务），请等待现有任务完成')
        try:c.execute('INSERT INTO jobs(id,status,created,record,idem,owner) VALUES(?,?,?,?,?,?)',
            (jid,'queued',time.time(),json.dumps(j,ensure_ascii=False),idem,owner))
        except sqlite3.IntegrityError:
            raise HTTPException(409,'请求已提交，请查询任务列表') from None
    return j

@app.get('/api/jobs',summary='列出最近100个任务')
def list_jobs(request:Request):
    with db() as c:rows=c.execute('SELECT record FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 100',
        (request.state.username,)).fetchall()
    return {'jobs':[json.loads(r[0]) for r in rows]}

@app.get('/api/jobs/{jid}',summary='查询任务状态与输出链接')
def job_status(jid:str,request:Request):return get_job(jid,owner=request.state.username)

@app.post('/api/jobs/{jid}/cancel',summary='取消指定任务',
    description='排队任务立即取消；运行任务转为cancel_requested，由工作线程按GPU任务ID取消。')
def cancel_job(jid:str,request:Request):
    with ACTIVE_LOCK:
        j=get_job(jid,owner=request.state.username)
        if j['status']=='queued':j.update(status='cancelled',error='已取消');put_job(j)
        elif j['status']=='running':j.update(status='cancel_requested');put_job(j)
    return j

@app.get('/api/jobs/{jid}/output',summary='下载已成功任务的MP4')
def job_output(jid:str,request:Request):
    j=get_job(jid,owner=request.state.username)
    if j['status']!='succeeded':raise HTTPException(404,'该任务还没有可用结果')
    return FileResponse(DATA/'outputs'/(jid+'.mp4'),media_type='video/mp4',filename='H3-'+jid[:8]+'.mp4')

@app.get('/api/jobs/{jid}/audio',summary='下载已成功有声任务的独立FLAC')
def job_audio(jid:str,request:Request):
    j=get_job(jid,owner=request.state.username)
    if j['status']!='succeeded' or not j.get('audio_output_url'):raise HTTPException(404,'该任务没有独立音频输出')
    return FileResponse(DATA/'outputs'/(jid+'.flac'),media_type='audio/flac',filename='H3-'+jid[:8]+'.flac')

def execute_job(j,c):
    from comfy_workflow import build_workflow
    args=j['request'];ids=list(dict.fromkeys([*args['inputs']['images'],*args['inputs']['videos'],*args['inputs']['audios'],
        *[v for v in (args['inputs']['first_frame'],args['inputs']['last_frame']) if v],
        *[g['media_id'] for g in args.get('guides',[])]]))
    uploads={u:upload_info(u,owner=j.get('owner',LEGACY_OWNER)) for u in ids};filenames={}
    for uid,u in uploads.items():
        path=pathlib.Path(u['model_path'])
        with path.open('rb') as f:r=c.post(COMFY+'/upload/image',files={'image':(path.name,f,'application/octet-stream')},data={'type':'input','overwrite':'true'})
        r.raise_for_status();n=r.json();filenames[uid]=(n.get('subfolder','').strip('/')+'/' if n.get('subfolder') else '')+n['name']
    args={**args,'_job_id':j['id']};graph=build_workflow(args,uploads,filenames)
    (DATA/'workflows'/(j['id']+'.json')).write_text(json.dumps(graph,ensure_ascii=False,indent=2),encoding='utf-8')
    # Do not attach this personal workbench to a pre-existing busy Comfy queue.
    if not comfy_queue_idle(c):raise RuntimeError('GPU队列已有其他任务，请检查ComfyUI；未提交新任务')
    if get_job(j['id'])['status']=='cancel_requested':j.update(status='cancelled',error='提交GPU前已取消');return
    r=c.post(COMFY+'/prompt',json={'prompt':graph,'client_id':'h3-studio'});r.raise_for_status()
    pid=r.json()['prompt_id'];j['comfy_prompt_id']=pid;put_job(j);start=time.monotonic()
    while not STOP.is_set() and time.monotonic()-start<3600:
        fresh=get_job(j['id'])
        if fresh['status']=='cancel_requested':
            dispatched=cancel_comfy_prompt(c,pid)
            j.update(status='cancelled',comfy_cancel_dispatched=dispatched,
                error='已请求取消此GPU任务' if dispatched else '已取消；此GPU任务已结束或不存在');return
        history=c.get(COMFY+'/history/'+pid).json()
        if pid in history:
            record=history[pid]
            (DATA/'workflows'/(j['id']+'.history.json')).write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
            if not record.get('status',{}).get('completed'):
                raise RuntimeError('ComfyUI推理失败；具体诊断已保存在本地任务history')
            files=saved_job_outputs(graph,record,j['id'],'SaveVideo',('videos','images','gifs'),'.mp4')
            if not files:raise RuntimeError('推理返回完成但没有此任务SaveVideo节点的MP4输出')
            output=files[0];target=DATA/'outputs'/(j['id']+'.raw.mp4')
            query={k:output[k] for k in ('filename','subfolder','type') if k in output}
            with c.stream('GET',COMFY+'/view?'+urlencode(query)) as rr:
                rr.raise_for_status()
                with target.open('wb') as f:
                    for chunk in rr.iter_bytes():f.write(chunk)
            final=DATA/'outputs'/(j['id']+'.mp4')
            # Native H3 length grid is approximate; deliver the explicit user-requested seconds.
            # Re-encode the exact requested frames: stream-copy can retain future
            # B-frames past -t and deliver a longer clip despite the trim request.
            ff=['-i',str(target),'-t',str(args['duration']),'-frames:v',str(args['duration']*24),
                '-c:v','libx264','-preset','veryfast','-crf',str(args.get('export_crf',18)),'-pix_fmt','yuv420p']
            ff+=['-c:a','aac'] if args['generate_audio'] else ['-an']
            run_ffmpeg([*ff,'-movflags','+faststart',str(final)])
            output_meta=probe(final)
            validate_output_video(output_meta,args)
            if args['generate_audio']:validate_output_audio(output_meta,args['duration'],'MP4')
            run_ffmpeg(['-xerror','-i',str(final),'-map','0:v:0','-map','0:a:0?','-f','null','-'])
            # Native AudioSaveHelper publishes singular "audio"; retain compatibility
            # with legacy plural producers without downloading the same entry twice.
            audio_files=saved_job_outputs(graph,record,j['id'],'SaveAudioAdvanced',('audio','audios'),'.flac')
            if args['generate_audio']:
                if not audio_files:raise RuntimeError('有声任务缺少独立FLAC音频输出')
                audio_file=audio_files[0];audio_raw=DATA/'outputs'/(j['id']+'.raw.flac')
                audio_target=DATA/'outputs'/(j['id']+'.flac')
                query={k:audio_file[k] for k in ('filename','subfolder','type') if k in audio_file}
                with c.stream('GET',COMFY+'/view?'+urlencode(query)) as ar:
                    ar.raise_for_status()
                    with audio_raw.open('wb') as f:
                        for chunk in ar.iter_bytes():f.write(chunk)
                run_ffmpeg(['-xerror','-i',str(audio_raw),'-t',str(args['duration']),'-c:a','flac',str(audio_target)])
                audio_meta=probe(audio_target)
                validate_output_audio(audio_meta,args['duration'],'FLAC',flac=True)
                j['audio_output_url']=f'/api/jobs/{j["id"]}/audio'
                j['audio_output_metadata']=audio_meta
            if get_job(j['id'])['status']=='cancel_requested':
                dispatched=cancel_comfy_prompt(c,pid)
                j.update(status='cancelled',output_url=None,audio_output_url=None,
                    comfy_cancel_dispatched=dispatched,error='结果导出期间已取消');return
            j.update(status='succeeded',progress=1,output_url=f'/api/jobs/{j["id"]}/output',
                elapsed_seconds=round(time.monotonic()-start,3),output_metadata=output_meta,
                output_sha256=hashlib.sha256(final.read_bytes()).hexdigest())
            return
        j['elapsed_seconds']=round(time.monotonic()-start,1);put_job(j)
        STOP.wait(3)
    j['comfy_cancel_dispatched']=cancel_comfy_prompt(c,pid)
    raise RuntimeError('任务超过1小时或本地服务关闭，已按任务ID请求取消')

def worker():
    global BLOCKED
    while not STOP.is_set():
        if BLOCKED:STOP.wait(3);continue
        with ACTIVE_LOCK:
            with db() as c:row=c.execute("SELECT record FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row:j=json.loads(row[0]);j.update(status='running',started_at=utc());put_job(j)
        if not row:STOP.wait(1);continue
        try:
            with httpx.Client(timeout=httpx.Timeout(60,read=90),trust_env=False) as client:
                try:execute_job(j,client)
                finally:
                    # Ensure an interrupted/failed task is actually idle before processing the next.
                    try:
                        for _ in range(15):
                            if comfy_queue_idle(client):break
                            STOP.wait(2)
                        else:BLOCKED='GPU队列尚未清空，已停止接收新任务；请检查原任务'
                    except Exception:
                        BLOCKED='GPU队列状态无法核验，已停止接收新任务；请检查原任务'
                        raise
        except Exception as e:
            if isinstance(e,(RuntimeError,HTTPException)):error=str(e.detail) if isinstance(e,HTTPException) else str(e)
            else:error='GPU连接／执行失败（'+type(e).__name__+'），请检查服务状态'
            j.update(status='failed',error=error)
        finally:j['finished_at']=utc();put_job(j)
        if BLOCKED:STOP.wait(3)

class OwnedStaticFiles(StaticFiles):
    async def get_response(self,path,scope):
        # StaticFiles normalizes OS paths, including Windows backslashes/case and
        # filesystem aliases. Authorize the actual file identity, not one URL.
        full_path,stat_result=self.lookup_path(path)
        starter=ROOT/'web'/'h3-ref-bf16-workflow.json'
        try:protected=bool(stat_result and os.path.samefile(full_path,starter))
        except OSError:protected=False
        if protected:
            user=authenticated_user(Request(scope))
            if user!='superdan':
                return JSONResponse({'detail':'请先输入用户名登录' if not user else '文件不存在'},
                    status_code=401 if not user else 404,headers={'Cache-Control':'private, no-store','Vary':'Cookie'})
        response=await super().get_response(path,scope)
        if protected:response.headers['Cache-Control']='private, no-store';response.headers['Vary']='Cookie'
        return response

app.mount('/',OwnedStaticFiles(directory=ROOT/'web',html=True),name='web')

_default_openapi=app.openapi
def authenticated_openapi():
    schema=_default_openapi()
    schema.setdefault('components',{}).setdefault('securitySchemes',{})['TestSession']={
        'type':'apiKey','in':'cookie','name':SESSION_COOKIE,
        'description':'POST /api/auth/login verifies username/password and returns an HttpOnly cookie.' if AUTH_MODE=='password' else 'POST /api/auth/login exchanges an allowlisted test username for an HttpOnly cookie; this is not proof of identity.'}
    for path,operations in schema['paths'].items():
        if path.startswith('/api/') and path not in ('/api/auth/login','/api/auth/logout','/api/auth/config'):
            for operation in operations.values():
                if isinstance(operation,dict):operation['security']=[{'TestSession':[]}]
    return schema
app.openapi=authenticated_openapi
