"""Update the existing systemd VPS without changing .env, proxy, or bot settings.

Uploads application code only. Validates an isolated staging instance, keeps a
server-side SQLite/code backup, and restores the previous code on startup failure.
"""

from __future__ import annotations

import io
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from scripts.deploy_vps import connect, load_env

ROOT = Path(__file__).resolve().parents[1]

REMOTE_PROGRAM = r"""
import hashlib, http.cookiejar, json, os, pathlib, shutil, sqlite3, subprocess, time, urllib.request, zipfile
root = pathlib.Path('/opt/cupcard').resolve()
release = root / 'releases' / release_id
backup = root / 'backups' / release_id
archive = root / 'releases' / (release_id + '.zip')
python = root / '.venv/bin/python'
assert root == pathlib.Path('/opt/cupcard') and (root / 'app/main.py').is_file()
assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected_hash
release.mkdir(mode=0o700)
with zipfile.ZipFile(archive) as bundle:
    for name in bundle.namelist():
        target = (release / name).resolve()
        assert target.is_relative_to(release) and target.parts[len(release.parts)] in {'app', 'alembic', 'DESIGN.md'}
    bundle.extractall(release)

subprocess.run([str(python), '-m', 'compileall', '-q', str(release / 'app')], check=True)
stage_script = release / 'preview_check.py'
stage_script.write_text("from app.main import create_app\nfrom app.config import Settings\nimport uvicorn\nuvicorn.run(create_app(Settings(_env_file=None, app_env='test', max_bot_token='stage-only', webhook_secret='stage-only', subscribe_on_startup=False)), host='127.0.0.1', port=8766)\n")
log = open(release / 'staging.log', 'w')
stage = subprocess.Popen([str(python), str(stage_script)], cwd=release, stdout=log, stderr=log)
def get(url, opener=None):
    with (opener.open(url, timeout=12) if opener else urllib.request.urlopen(url, timeout=12)) as response:
        return response.read()
try:
    for attempt in range(40):
        if stage.poll() is not None: raise RuntimeError('Staging process exited; live version untouched')
        try:
            get('http://127.0.0.1:8766/health')
            break
        except Exception: time.sleep(.5)
    else: raise RuntimeError('Staging health timeout; live version untouched')
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base = 'http://127.0.0.1:8766'
    for role, pages in [('client',['/me','/me/league','/me/shops','/me/qr','/settings','/biz/apply']), ('business',['/biz','/biz/promos','/biz/staff','/biz/earn','/biz/scan','/settings'])]:
        get(urllib.request.Request(base+'/login/demo', data=('role='+role).encode()), opener)
        for page in pages:
            assert b'design.css?v=29' in get(base+page, opener), page
            assert b'theme-toggle' in get(base+page, opener), page
    assert b'min-width: 900px' in get(base+'/static/css/design.css')
    print('STAGING_OK: client and business pages', flush=True)
finally:
    stage.terminate()
    try: stage.wait(timeout=10)
    except subprocess.TimeoutExpired: stage.kill(); stage.wait()
    log.close()

values={}
for line in (root/'.env').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        k,v=line.split('=',1); values[k.strip()]=v.strip().strip('"').strip("'")
assert values.get('APP_ENV') == 'production', 'Unexpected environment'
assert values.get('DATABASE_URL','').startswith('sqlite+aiosqlite:////opt/cupcard/data/'), 'Unexpected database; stop for inspection'
db=pathlib.Path(values['DATABASE_URL'].split('///',1)[1]).resolve()
assert db.is_relative_to(root/'data') and db.is_file()
backup.mkdir(parents=True, mode=0o700)
env_hash=hashlib.sha256((root/'.env').read_bytes()).hexdigest()
subprocess.run(['systemctl','stop','cupcard.service'],check=True)
swapped=[]
try:
    with sqlite3.connect(db) as source, sqlite3.connect(backup/'cupcard.db') as dest:
        source.backup(dest)
        before = source.execute('select count(*) from customers').fetchone()[0]
    shutil.copy2(root/'.env', backup/'.env')
    os.chmod(backup/'.env', 0o600)
    for name in ['app','alembic']:
        if (root/name).exists(): os.replace(root/name,backup/name)
        swapped.append(name)
        os.replace(release/name,root/name)
    if (release/'DESIGN.md').exists(): shutil.copy2(release/'DESIGN.md',root/'DESIGN.md')
    assert hashlib.sha256((root/'.env').read_bytes()).hexdigest() == env_hash
    subprocess.run(['systemctl','start','cupcard.service'],check=True)
    for attempt in range(40):
        try:
            health=json.loads(get('http://127.0.0.1:8000/health'))
            assert health.get('ok') and health.get('postgres'), 'Database health failed'
            assert b'design.css?v=29' in get('http://127.0.0.1:8000/login')
            break
        except Exception: time.sleep(.5)
    else: raise RuntimeError('Updated service failed health checks')
    with sqlite3.connect(db) as conn:
        assert conn.execute('select count(*) from customers').fetchone()[0] >= before
        tables={row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
        assert {'challenges','challenge_claims','business_locations','shop_staff','shop_invites','promo_links','platform_admins'} <= tables
        biz_cols={row[1] for row in conn.execute('pragma table_info(businesses)')}
        assert {'inn','director_name','verified_at','website','status','org_name','parent_id'} <= biz_cols
        staff_cols={row[1] for row in conn.execute('pragma table_info(shop_staff)')}
        assert {'schedule_days','shift_from','shift_to'} <= staff_cols
        promo_cols={row[1] for row in conn.execute('pragma table_info(loyalty_programs)')}
        assert 'archived_at' in promo_cols
    print(json.dumps({'deployed':release_id,'backup':str(backup),'public_url':values.get('PUBLIC_BASE_URL'), 'database_preserved':True,'settings_unchanged':True}),flush=True)
except Exception:
    subprocess.run(['systemctl','stop','cupcard.service'],check=False)
    for name in reversed(swapped):
        if (root/name).exists(): os.replace(root/name,release/(name+'-failed'))
        if (backup/name).exists(): os.replace(backup/name,root/name)
    subprocess.run(['systemctl','start','cupcard.service'],check=True)
    print('ROLLED_BACK: previous code restored; additive database tables retained',flush=True)
    raise
"""


def main():
    import hashlib

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    release_id = datetime.now(UTC).strftime("ui-%Y%m%d-%H%M%S")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        paths = [ROOT / "DESIGN.md"]
        for directory in ("app", "alembic"):
            paths.extend((ROOT / directory).rglob("*"))
        for path in paths:
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                bundle.write(path, path.relative_to(ROOT).as_posix())
    archive = buffer.getvalue()
    client = connect(load_env())
    try:
        _, out, _err = client.exec_command(
            "install -d -m 700 /opt/cupcard/releases /opt/cupcard/backups", timeout=20
        )
        if out.channel.recv_exit_status() != 0:
            raise RuntimeError("Could not create release directories")
        with client.open_sftp() as sftp, sftp.file(
            f"/opt/cupcard/releases/{release_id}.zip", "wb"
        ) as dest:
            dest.write(archive)
        program = (
            f"release_id={release_id!r}\nexpected_hash={hashlib.sha256(archive).hexdigest()!r}\n"
            + REMOTE_PROGRAM
        )
        stdin, stdout, stderr = client.exec_command("python3 -u -", timeout=180)
        stdin.write(program)
        stdin.flush()
        stdin.channel.shutdown_write()
        for line in stdout:
            print(line.rstrip(), flush=True)
        errors = stderr.read().decode("utf-8", "replace")
        status = stdout.channel.recv_exit_status()
        if status:
            print(errors[-2400:])
            raise SystemExit(status)
    finally:
        client.close()


if __name__ == "__main__":
    main()
