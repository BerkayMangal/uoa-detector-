"""Public pairing requires a click and never persists or renders its capability."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "webapp/static/theta_pairing.js"
TOKEN = "a" * 64
FILENAME = "uoa-theta-hazir-012345abcdef.py"


def test_public_landing_has_no_auth_form_tracking_or_external_dependencies() -> None:
    env = Environment(loader=FileSystemLoader(ROOT / "webapp/templates"), undefined=StrictUndefined)
    text = env.get_template("theta_pairing.html").render()
    assert 'content="noindex, nofollow, nosnippet"' in text
    assert 'name="referrer" content="no-referrer"' in text
    assert 'rel="icon" href="data:,"' in text
    assert "http://" not in text and "https://" not in text
    assert "<input" not in text and "<form" not in text
    assert "live-alerts.js" not in text
    assert "UOA şifresi veya ThetaData anahtarı sorulmaz" in text
    assert "bağlantı dosyası 7 gün geçerlidir" in text
    assert 'id="theta-pairing-download" type="button" disabled' in text
    assert 'id="theta-pairing-command"' in text
    assert "Dosyayı indirdikten sonra komut burada görünecek" in text
    assert "python3 ~/Downloads/" not in text


HARNESS = r"""
const fs=require('fs');
const scenario=JSON.parse(process.argv[2]);
const nodes={},requests=[],replacements=[],downloads=[],revoked=[];
class Element {
  constructor(tag='div') { this.tag=tag;this.textContent='';this.dataset={};this.disabled=true;this.listeners={};this.className='hidden'; }
  addEventListener(name,fn){this.listeners[name]=fn;}
  click(){downloads.push({href:this.href,download:this.download,rel:this.rel});}
  remove(){}
}
const node=id=>nodes[id]||(nodes[id]=new Element());
global.document={getElementById:node,createElement:tag=>new Element(tag),body:{append(){}}};
global.window={location:{hash:scenario.hash,pathname:'/theta-bagla'},history:{replaceState:(state,title,path)=>{replacements.push(path);window.location.hash='';}}};
global.localStorage=new Proxy({}, {get(){throw new Error('persistent storage forbidden');}});
global.sessionStorage=global.localStorage;
global.URL={createObjectURL:()=> 'blob:local-download',revokeObjectURL:value=>revoked.push(value)};
global.setTimeout=fn=>fn();
global.fetch=async(url,options)=>{
  requests.push({url,options});
  if(scenario.networkError)throw new Error('network unavailable');
  return {ok:scenario.status===200,status:scenario.status,headers:{get:()=>scenario.disposition},blob:async()=>({size:scenario.empty?0:100})};
};
eval(fs.readFileSync(process.argv[1],'utf8'));
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const button=node('theta-pairing-download');
  if(scenario.click && button.listeners.click){
    button.listeners.click();
    if(scenario.doubleClick)button.listeners.click();
    await settle();await settle();
  }
  console.log(JSON.stringify({nodes:Object.fromEntries(Object.entries(nodes).map(([k,v])=>[k,{text:v.textContent,dataset:v.dataset,disabled:v.disabled,className:v.className}])),requests,replacements,hash:window.location.hash,downloads,revoked}));
})();
"""


def run_pairing(**scenario: object) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the browser-script contract test")
    result = subprocess.run(
        [node, "-e", HARNESS, str(SCRIPT), json.dumps({
            "hash": "#" + TOKEN, "status": 200,
            "disposition": f'attachment; filename="{FILENAME}"', **scenario,
        })],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def test_fragment_is_cleared_without_request_until_user_clicks() -> None:
    result = run_pairing()
    assert result["hash"] == ""
    assert result["replacements"] == ["/theta-bagla"]
    assert result["requests"] == [] and result["downloads"] == []
    assert result["nodes"]["theta-pairing-download"]["disabled"] is False
    assert TOKEN not in json.dumps(result["nodes"])


def test_click_downloads_once_without_ambient_login_and_without_token_in_url() -> None:
    result = run_pairing(click=True, doubleClick=True)
    assert len(result["requests"]) == 1
    request = result["requests"][0]
    assert request["url"] == "/api/theta/pairing/redeem"
    assert request["options"]["credentials"] == "omit"
    assert request["options"]["redirect"] == "error"
    assert json.loads(request["options"]["body"]) == {"pairing_token": TOKEN}
    assert result["downloads"] == [{"href": "blob:local-download", "download": FILENAME, "rel": "noreferrer"}]
    assert result["nodes"]["theta-pairing-command"]["text"] == f"python3 ~/Downloads/{FILENAME} --watch"
    assert result["revoked"] == ["blob:local-download"]
    assert TOKEN not in json.dumps(result["nodes"])


@pytest.mark.parametrize("status", [400, 404, 409, 410])
def test_used_or_expired_link_requires_new_link_without_auth_prompt(status: int) -> None:
    result = run_pairing(click=True, status=status)
    assert result["downloads"] == []
    assert "yeni bağlantı hazırla" in result["nodes"]["theta-pairing-status"]["text"]
    assert result["nodes"]["theta-pairing-download"]["disabled"] is True
    assert result["nodes"]["theta-pairing-renew"]["className"] == ""


@pytest.mark.parametrize("fragment", ["", "#wrong", "#" + "a" * 65])
def test_invalid_token_never_enables_redemption(fragment: str) -> None:
    result = run_pairing(hash=fragment, click=True)
    assert result["requests"] == [] and result["downloads"] == []
    assert result["nodes"]["theta-pairing-download"]["disabled"] is True
    assert result["hash"] == ""


def test_ambiguous_network_failure_does_not_auto_retry_or_claim_download_success() -> None:
    result = run_pairing(click=True, networkError=True)
    assert len(result["requests"]) == 1 and result["downloads"] == []
    assert "İndirme doğrulanamadı" in result["nodes"]["theta-pairing-status"]["text"]
    assert result["nodes"]["theta-pairing-download"]["disabled"] is True


@pytest.mark.parametrize("disposition", [
    None,
    'attachment; filename="uoa-theta-baglanti.py"',
    'attachment; filename="../uoa-theta-hazir-012345abcdef.py"',
    'attachment; filename="uoa-theta-hazir-012345abcdeF.py"',
    'attachment; filename="uoa-theta-hazir-012345abcdef.py"; filename="other.py"',
    'attachment; filename="uoa-theta-hazir-012345abcdef.py;touch bad"',
    'inline; filename="uoa-theta-hazir-012345abcdef.py"',
])
def test_malformed_attachment_never_downloads_or_displays_a_runnable_command(
    disposition: str | None,
) -> None:
    result = run_pairing(click=True, disposition=disposition)
    assert result["downloads"] == [] and result["revoked"] == []
    assert "python3" not in result["nodes"]["theta-pairing-command"]["text"]
    assert "adı doğrulanamadı" in result["nodes"]["theta-pairing-status"]["text"]
