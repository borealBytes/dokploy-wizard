"""Secret-safe remote execution of retained Coder workspace smoke proofs."""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass

from dokploy_wizard.proof.model_sync_artifacts import require_mapping
from dokploy_wizard.proof.model_sync_upgrade_host_a_types import UpgradeHostAError
from dokploy_wizard.remote import capture_remote_output
from dokploy_wizard.remote_transport import ParamikoRemoteTransport

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
