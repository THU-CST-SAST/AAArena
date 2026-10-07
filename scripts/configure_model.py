#!/usr/bin/env python3
"""Create a user-owned profile; credentials always stay in environment variables."""
import argparse,json,os
from pathlib import Path

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--name',required=True);p.add_argument('--model',required=True)
 p.add_argument('--base-url-env',default='AA_ARENA_BASE_URL')
 p.add_argument('--api-key-env',default='AA_ARENA_API_KEY')
 p.add_argument('--directory',type=Path,default=Path(os.environ.get('AA_ARENA_PROFILE_DIR',Path.home()/'.config/aa-arena/models')))
 p.add_argument('--context-window',type=int,default=200000)
 p.add_argument('--reasoning-effort',choices=('max','high'),default='max')
 p.add_argument('--stream-max-retries',type=int,help='Override native Codex stream retries; use 0 for runtime-owned recovery')
 p.add_argument('--claude-auth-mode',choices=('api_key','bearer'),default='api_key')
 a=p.parse_args()
 if a.stream_max_retries is not None and a.stream_max_retries<0:p.error("stream-max-retries must be nonnegative")
 if Path(a.name).name!=a.name or a.name in {'.','..'}:p.error('name must be a simple filename')
 url=os.environ.get(a.base_url_env)
 if not url or not url.startswith(('http://','https://')):p.error('Set '+a.base_url_env+' to your API base URL')
 a.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
 dest=a.directory/(a.name+'.json')
 record={'name':a.name,'model':a.model,'model_provider':'user-provider','base_url':url,'api_key_env':a.api_key_env,'reasoning_effort':a.reasoning_effort,'wire_api':'responses','context_window':a.context_window,'effective_context_window_percent':95}
 record['claude_auth_mode']=a.claude_auth_mode
 record['stream_max_retries']=a.stream_max_retries
 # Never overwrite an existing profile silently.
 with dest.open('x') as f:json.dump(record,f,indent=2);f.write('\n')
 dest.chmod(0o600)
 print('Profile created:',dest)
 print('Set AA_ARENA_PROFILE_DIR to',a.directory)
 print('Set the credential in',a.api_key_env,'before running. No API key was read or stored.')
if __name__=='__main__':main()
