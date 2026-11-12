"""The product view never turns unknown option results into zero or markup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "webapp/static/theta_validation.js"


def render() -> str:
    env = Environment(
        loader=FileSystemLoader(ROOT / "webapp/templates"),
        autoescape=select_autoescape(["html"]), undefined=StrictUndefined,
    )
    return env.get_template("theta_validation.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/opsiyon-dogrulama")),
        page_title=lambda _: "Opsiyon doğrulama | UOA", seo_description="UOA",
        alerts_disabled=True, glossary={},
    )


def test_initial_page_is_private_and_waiting_for_real_connection() -> None:
    text = render()
    assert 'content="noindex, nofollow, nosnippet"' in text
    assert "ThetaData bağlantısı bekleniyor" in text
    assert "Henüz doğrulanmış ThetaData kotasyonu alınmadı" in text
    assert "Geriye dönük inceleme" in text
    assert "Gerçek emir veya gerçekleşmiş işlem değildir" in text
    assert "UTC olarak kaydedilir" in text
    assert 'type="password"' not in text
    assert 'id="theta-pairing-create"' in text
    assert "Şifresiz Mac bağlantısı hazırla" in text
    assert "UOA şifresi veya ThetaData anahtarı sorulmaz" in text
    assert "bağlantı dosyası 7 gün geçerlidir" in text
    assert "yeni dosyana özel komutu" in text
    assert "python3 ~/Downloads/uoa-theta-baglanti.py" not in text


HARNESS = r"""
const fs = require('fs');
class E {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.dataset={}; this.listeners={}; this.value=''; this.disabled=false; this.className=''; this._text=''; }
  set textContent(v) { this._text=String(v); this.children=[]; }
  get textContent() { return this._text+this.children.map(c=>c.textContent).join(' '); }
  set innerHTML(v) { throw new Error('unsafe HTML sink'); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; this._text=''; }
  get firstChild() { return this.children[0]; }
  addEventListener(name,fn) { this.listeners[name]=fn; }
  querySelector(selector) { return this.children.find(c=>c.className.split(' ').includes(selector.slice(1))) || null; }
}
const nodes={};
const node=id=>nodes[id]||(nodes[id]=new E());
const root=node('theta-validation');
const connection=new E(); connection.className='theta-connection'; root.append(connection);
global.document={getElementById:node,createElement:tag=>new E(tag)};
const scenario=JSON.parse(process.argv[2]);
const calls=[];
const navigations=[];
global.window={location:{assign:value=>navigations.push(value)}};
global.fetch=async(url,options)=>{
  calls.push({url,options});
  if (scenario.fail) return {ok:false,status:503};
  return {ok:true,status:200,json:async()=>url.endsWith('/status')?scenario.status:url.endsWith('/sources')?{sources:scenario.sources||[]}:url.endsWith('/pairings')?scenario.pairing:{} };
};
eval(fs.readFileSync(process.argv[1],'utf8'));
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await settle(); await settle();
  if (scenario.manual) {
    node('theta-symbol').value=scenario.manual.symbol;
    node('theta-entry').value=scenario.manual.time;
    node('theta-manual-form').listeners.submit({preventDefault(){}});
    await settle(); await settle();
  }
  if (scenario.retry) {
    const card=node('theta-results').children[0];
    const retry=card.children.find(child=>child.tag==='button');
    retry.listeners.click();
    await settle(); await settle();
  }
  if (scenario.pairing) {
    node('theta-pairing-create').listeners.click();
    await settle(); await settle();
  }
  console.log(JSON.stringify({text:Object.fromEntries(Object.entries(nodes).map(([k,v])=>[k,v.textContent])),calls,connection:connection.dataset,navigations}));
})();
"""


def run_ui(scenario: dict[str, object]) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the browser-script contract test")
    run = subprocess.run(
        [node, "-e", HARNESS, str(SCRIPT), json.dumps(scenario)],
        cwd=ROOT, capture_output=True, text=True, check=True,
        env={**os.environ, "TZ": "America/Los_Angeles"},
    )
    return json.loads(run.stdout)


def job() -> dict[str, object]:
    return {
        "request_id": "theta_test", "option_symbol": "TSLA261127C00410000",
        "planned_entry_at": "2026-11-11T15:24:20Z", "source_kind": "manual_research",
        "state": "pending",
    }


def test_missing_net_is_unknown_while_actual_negative_server_result_is_shown() -> None:
    value = run_ui({"status": {
        "status": "data_received", "last_connector_contact": "2026-11-12T10:00:00Z",
        "jobs": [job()], "recent_results": [{
            "request_id": "theta_test", "received_at": "2026-11-12T10:00:00Z", "status": "PARTIAL",
            "result": {"source": "thetadata", "status": "PARTIAL", "markouts": [
                {"horizon_minutes": 15, "status": "UNKNOWN", "modeled_net_pnl_usd": None},
                {"horizon_minutes": 60, "status": "EVALUATED", "modeled_net_pnl_usd": "-88.30"},
            ]},
        }],
    }})
    text = value["text"]["theta-results"]
    assert "Ölçülemedi" in text and "88,30" in text
    assert "0,00" not in text
    assert "Kısmi ölçüm" in text and "Elle seçilmiş tarihsel kontrat" in text


def test_untrusted_source_text_cannot_create_html_and_missing_connection_is_not_connected() -> None:
    hostile = '<img src=x onerror="alert(1)">'
    row = job()
    row["option_symbol"] = hostile
    value = run_ui({"status": {"status": "awaiting_connection", "jobs": [row], "recent_results": []}})
    assert hostile in value["text"]["theta-results"]
    assert value["connection"]["connectionState"] == "awaiting_connection"
    assert value["text"]["theta-connection-title"] == "ThetaData bağlantısı bekleniyor"


def test_unreadable_api_does_not_claim_empty_records_or_connected_provider() -> None:
    value = run_ui({"fail": True})
    assert "okunamadı" in value["text"]["theta-connection-title"]
    assert "sonuçlar bilinmiyor" in value["text"]["theta-results"]
    assert "Henüz inceleme yok" not in value["text"]["theta-results"]


def test_manual_time_is_explicit_utc_even_on_a_non_utc_device() -> None:
    value = run_ui({
        "status": {"status": "awaiting_connection", "jobs": [], "recent_results": []},
        "manual": {"symbol": "TSLA261127C00410000", "time": "2026-11-11T15:24:20"},
    })
    posts = [row for row in value["calls"] if row["options"]["method"] == "POST"]
    assert len(posts) == 1
    payload = json.loads(posts[0]["options"]["body"])
    assert payload["planned_entry_at"] == "2026-11-11T15:24:20.000Z"
    assert payload["source_kind"] == "manual_research"
    assert "pnl" not in payload


def test_a_completed_job_missing_from_recent_results_is_not_shown_as_waiting() -> None:
    row = job()
    row.update(state="completed", finalized=True)
    value = run_ui({"status": {"status": "data_received", "jobs": [row], "recent_results": []}})
    assert "Sonuç bu listede okunamadı" in value["text"]["theta-results"]
    assert "Kotasyon bekleniyor" not in value["text"]["theta-results"]


def test_retry_limit_requires_click_and_posts_only_to_linked_retry_endpoint() -> None:
    row = job()
    row["state"] = "retry_limit"
    scenario = {"status": {"status": "connection_or_data_error", "jobs": [row], "recent_results": []}}
    initial = run_ui(scenario)
    assert not any(call["options"]["method"] == "POST" for call in initial["calls"])
    clicked = run_ui({**scenario, "retry": True})
    posts = [call for call in clicked["calls"] if call["options"]["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["url"] == "/api/theta/requests/theta_test/retry"


def test_authenticated_page_hands_pairing_token_over_only_in_fragment() -> None:
    token = "a" * 64
    value = run_ui({
        "status": {"status": "awaiting_connection", "jobs": [], "recent_results": []},
        "pairing": {"pairing_token": token, "expires_at": "2026-11-12T15:00:00Z", "request_count": 2},
    })
    posts = [call for call in value["calls"] if call["options"]["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["url"] == "/api/theta/pairings"
    assert posts[0]["options"]["credentials"] == "same-origin"
    assert value["navigations"] == ["/theta-bagla#" + token]
    assert token not in json.dumps(value["text"])
