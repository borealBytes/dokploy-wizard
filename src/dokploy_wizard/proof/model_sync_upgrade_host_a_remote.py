"""Secret-safe remote execution of retained Coder workspace smoke proofs."""

from __future__ import annotations

import base64
import json
import shlex
import time
from dataclasses import dataclass
from typing import Final

from dokploy_wizard.dokploy.coder_migration_api import (
    CoderHttpRequest,
    CoderHttpResponse,
    CoderTransportError,
)
from dokploy_wizard.dokploy.coder_migration_types import CoderProtocolError
from dokploy_wizard.proof.model_sync_artifacts import require_mapping, require_text
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import UpgradeHostAError
from dokploy_wizard.remote import capture_remote_output
from dokploy_wizard.remote_transport import ParamikoRemoteTransport

_REMOTE_CODER_RESPONSE_LIMIT: Final = 1024 * 1024
_REMOTE_CODER_API_SCRIPT: Final = r"""
import base64,ipaddress,json,subprocess,sys,urllib.error,urllib.request
network,service,limit=sys.argv[1],sys.argv[2],int(sys.argv[3])
payload=json.load(sys.stdin)
containers=subprocess.run(
    ['docker','ps','--filter',f'label=com.docker.compose.service={service}',
     '--filter','status=running','--format','{{.ID}}'],
    check=True,capture_output=True,text=True,
).stdout.splitlines()
if len(containers)!=1: raise RuntimeError('coder container')
inspected=json.loads(subprocess.check_output(['docker','inspect',containers[0]]))
address=inspected[0]['NetworkSettings']['Networks'][network]['IPAddress']
parsed=ipaddress.ip_address(address)
if not parsed.is_private: raise RuntimeError('coder address')
authority=f'[{address}]' if parsed.version==6 else address
body=payload.get('body')
data=None if body is None else base64.b64decode(body,validate=True)
request=urllib.request.Request(
    f"http://{authority}:3000{payload['path']}",data=data,
    headers=dict(payload['headers']),method=payload['method'],
)
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*_args,**_kwargs): return None
try:
    response=urllib.request.build_opener(NoRedirect()).open(request,timeout=30)
except urllib.error.HTTPError as error:
    response=error
raw=response.read(limit+1)
if len(raw)>limit: raise RuntimeError('coder response')
json.dump({'status':response.status,'body':base64.b64encode(raw).decode('ascii')},sys.stdout)
sys.stdout.write('\n')
"""
_WORKSPACE_TEST_SCRIPT = r"""
const fs=require('fs'),http=require('http'),https=require('https');
const kind=process.argv[1];
let base,key,model,health=[];
if(kind==='ubuntu-vscode-opencode-pi'||kind==='ubuntu-vscode-opencode-web'){
  const c=JSON.parse(fs.readFileSync('/home/coder/.config/opencode/opencode.json'));
  const p=c.provider.litellm,o=p.options;
  base=o.baseURL;key=o.apiKey;model=Object.keys(p.models)[0];
  if(kind==='ubuntu-vscode-opencode-web')health=['http://127.0.0.1:4097/'];
}else if(kind==='ubuntu-vscode-hermes'){
  base=process.env.OPENAI_API_BASE;key=process.env.OPENAI_API_KEY;model=process.env.HERMES_MODEL;
  health=['http://127.0.0.1:9120/','http://127.0.0.1:8788/health'];
}else if(kind==='ubuntu-vscode-kdense-byok'){
  const lines=fs.readFileSync('/home/coder/.cache/kdense-byok-src/.env','utf8').split('\n');
  const e=Object.fromEntries(lines.filter(x=>x.includes('=')).map(x=>{
    const i=x.indexOf('=');return[x.slice(0,i),x.slice(i+1)];
  }));
  base=e.OPENAI_API_BASE;key=e.OPENAI_API_KEY;model=e.DEFAULT_AGENT_MODEL;
  health=['http://127.0.0.1:3000/health'];
}else process.exit(2);
const get=u=>new Promise((ok,no)=>{
  const x=new URL(u),client=x.protocol==='https:'?https:http;
  const r=client.get(x,z=>{
    z.resume();z.on('end',()=>z.statusCode>=200&&z.statusCode<400?ok():no(new Error('health')));
  });
  r.setTimeout(10000,()=>r.destroy());r.on('error',no);
});
const invoke=()=>new Promise((ok,no)=>{
  if(!base||!key||!model)return no(new Error('config'));
  const body=Buffer.from(JSON.stringify({
    model,messages:[{role:'user',content:'Reply with OK.'}],max_tokens:4
  }));
  const u=new URL(base.replace(/\/$/,'')+'/chat/completions');
  const client=u.protocol==='https:'?https:http;
  const r=client.request(u,{
    method:'POST',headers:{
      Authorization:'Bearer '+key,
      'Content-Type':'application/json',
      'Content-Length':body.length
    }
  },z=>{
    let n=0;z.on('data',b=>n+=b.length);
    z.on('end',()=>z.statusCode>=200&&z.statusCode<300&&n>0?ok():no(new Error('alias')));
  });
  r.setTimeout(30000,()=>r.destroy());r.on('error',no);r.end(body);
});
Promise.all(health.map(get)).then(invoke).then(()=>process.stdout.write('{"ok":true}\n')).catch(()=>process.exit(3));
"""


@dataclass(frozen=True, slots=True, repr=False)
class RemoteCoderTransport:
    """Carry Coder API requests over SSH to the private shared network."""

    host: str
    password: str
    stack_name: str

    def send(self, request: CoderHttpRequest) -> CoderHttpResponse:
        payload = json.dumps(
            {
                "body": None if request.body is None else base64.b64encode(request.body).decode(),
                "headers": request.headers,
                "method": request.method,
                "path": request.path,
            },
            separators=(",", ":"),
        ).encode()
        transport = ParamikoRemoteTransport.connect(
            hostname=self.host,
            username="root",
            password=self.password,
            remote_root="/root/dokploy-wizard",
            timeout=30,
        )
        try:
            output = capture_remote_output(
                transport,
                shlex.join(
                    [
                        "python3",
                        "-c",
                        _REMOTE_CODER_API_SCRIPT,
                        f"{self.stack_name}-shared",
                        f"{self.stack_name}-coder",
                        str(_REMOTE_CODER_RESPONSE_LIMIT),
                    ]
                ),
                timeout_seconds=60,
                stdin_bytes=payload,
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise CoderTransportError("Remote Coder transport failed") from error
        finally:
            transport.close()
        try:
            response = require_mapping(json.loads(output), "remote Coder response")
            status = response.get("status")
            if not isinstance(status, int) or isinstance(status, bool):
                raise CoderTransportError("Remote Coder response status is invalid")
            body = base64.b64decode(
                require_text(response.get("body"), "remote Coder response body"),
                validate=True,
            )
        except (ValueError, json.JSONDecodeError) as error:
            raise CoderTransportError("Remote Coder response is invalid") from error
        return CoderHttpResponse(status, body)

    def login(self, email: str, password: str) -> str:
        response = self.send(
            CoderHttpRequest(
                method="POST",
                path="/api/v2/users/login",
                body=json.dumps(
                    {"email": email, "password": password}, separators=(",", ":")
                ).encode(),
                headers=(
                    ("Accept", "application/json"),
                    ("Content-Type", "application/json"),
                ),
            )
        )
        if response.status not in {200, 201}:
            raise CoderProtocolError("Coder login failed")
        try:
            payload = require_mapping(json.loads(response.body), "Coder login response")
            return require_text(payload.get("session_token"), "Coder login session token")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CoderProtocolError("Coder login response is invalid") from error


@dataclass(frozen=True, slots=True, repr=False)
class RemoteWorkspaceTester:
    host: str
    password: str
    stack_name: str
    session_token: str

    def __call__(self, template_name: str, workspace_name: str) -> None:
        transport = ParamikoRemoteTransport.connect(
            hostname=self.host,
            username="root",
            password=self.password,
            remote_root="/root/dokploy-wizard",
            timeout=600,
        )
        try:
            service = shlex.quote(f"{self.stack_name}-coder")
            workspace_command = shlex.join(
                ["node", "-e", _WORKSPACE_TEST_SCRIPT, template_name]
            )
            coder_arguments = shlex.join(
                [
                    "sh",
                    "ssh",
                    "--disable-autostart",
                    workspace_name,
                    "--",
                    workspace_command,
                ]
            )
            command = (
                "set -eu; "
                f"container=$(docker ps --filter label=com.docker.compose.service={service} "
                "--filter status=running --format '{{.ID}}'); "
                "test -n \"$container\"; test \"$(printf '%s\\n' \"$container\" | wc -l)\" -eq 1; "
                "IFS= read -r CODER_SESSION_TOKEN; export CODER_SESSION_TOKEN; "
                "printf '%s\\n' \"$CODER_SESSION_TOKEN\" | "
                "docker exec -i -e CODER_URL=http://127.0.0.1:3000 \"$container\" "
                "sh -c 'IFS= read -r CODER_SESSION_TOKEN; export CODER_SESSION_TOKEN; "
                "exec /opt/coder \"$@\"' "
                f"{coder_arguments}"
            )
            output = ""
            for attempt in range(30):
                try:
                    output = capture_remote_output(
                        transport,
                        command,
                        timeout_seconds=600,
                        stdin_bytes=(self.session_token + "\n").encode(),
                    )
                except RuntimeError:
                    if attempt == 29:
                        raise
                    time.sleep(10)
                    continue
                break
        except (OSError, RuntimeError, ValueError) as error:
            raise UpgradeHostAError("Retained Coder workspace smoke proof failed") from error
        finally:
            transport.close()
        try:
            payload = require_mapping(json.loads(output), "workspace smoke proof")
        except (json.JSONDecodeError, ValueError) as error:
            raise UpgradeHostAError("Retained Coder workspace smoke proof is invalid") from error
        if payload != {"ok": True}:
            raise UpgradeHostAError("Retained Coder workspace smoke proof is incomplete")
