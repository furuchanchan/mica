from pathlib import Path
import subprocess, tempfile, plistlib, shutil, json, sys
HERE=Path(__file__).resolve().parent
SERVICE=HERE.parent/'service'  # このリポジトリの service/ を同梱する
FILES=('mica_config.py','mica_http.py','mica_llm.py','shodan_live.py','shodan_ai.py','shodan_transcribe.py','shodan_speakers.py','shodan_context.py','shodan_session.py','shodan_calendar.py','shodan_preparation.py','shodan_evidence.py','shodan_text.py','shodan_local_asr.py','shodan_asr_service.py','shodan_grounding.py','shodan_display.py','live.html')
missing=[f for f in FILES if not (SERVICE/f).is_file()]
if missing:raise SystemExit('service/ に足りないファイルがあります: '+', '.join(missing))
qa='--qa' in sys.argv
name='Mica QA' if qa else 'Mica'
build=Path(tempfile.mkdtemp(prefix='mica-build-'))
app=build/(name+'.app')
mac=app/'Contents/MacOS'; res=app/'Contents/Resources'
mac.mkdir(parents=True); res.mkdir()
info={'CFBundleExecutable':'Mica','CFBundleIdentifier':'app.mica.assistant.qa' if qa else 'app.mica.assistant', 'CFBundleName':name,'CFBundleDisplayName':name,'CFBundleVersion':'1','CFBundleShortVersionString':'0.12.1','CFBundlePackageType':'APPL','LSMinimumSystemVersion':'26.0','NSHighResolutionCapable':True,'CFBundleIconFile':'AppIcon','CFBundleURLTypes':[{'CFBundleURLName':'Mica meeting','CFBundleURLSchemes':['mica']}]}
(app/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
args=['swiftc','-module-cache-path','/private/tmp/mica-swift-cache','-parse-as-library','-O','-target','arm64-apple-macos26.0',str(HERE/'Mica.swift'),'-o',str(mac/'Mica')]
if qa: args[1:1]=['-D','QA']
subprocess.run(args,check=True)
for f in FILES:
    shutil.copyfile(SERVICE/f,res/f)

shutil.copyfile(HERE/'mica_service.py',res/'mica_service.py')
shutil.copyfile(HERE/'AppIcon.icns',res/'AppIcon.icns')
subprocess.run(['codesign','--force','--deep','--sign','-',str(app)],check=True)
subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)
(HERE/('qa-build.json' if qa else 'build.json')).write_text(json.dumps({'app':str(app),'verified':True}))
print(json.dumps({'app':str(app),'verified':True}))
