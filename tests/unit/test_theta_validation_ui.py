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
    assert "Otomatik opsiyon ölçümü" in text
    assert text.index('id="theta-device-pairing-create"') < text.index('id="theta-manual-form"')
    assert '<a class="theta-back" href="/">' in text
    assert '<a class="theta-back" href="/olcum">' in text


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
const intervals=[];
global.window={location:{assign:value=>navigations.push(value)},setInterval:(fn,ms)=>{intervals.push({fn,ms});return intervals.length;}};
global.fetch=async(url,options)=>{
  calls.push({url,options});
  if (scenario.fail) return {ok:false,status:503};
  if (url==='/api/theta/devices/status') return {ok:true,status:200,json:async()=>scenario.devices||{status:'awaiting_enrollment',devices:[]}};
  if (url.startsWith('/api/theta/automatic/status')) return {ok:true,status:200,json:async()=>scenario.automatic||{status:'ready',day:'2026-11-12',counts:{},recent:[],latest_report:null}};
  if (url==='/api/theta/devices/pairings') return {ok:true,status:200,json:async()=>scenario.devicePairing};
  if (url==='/api/theta/automatic/reruns') return {ok:true,status:200,json:async()=>scenario.rerunReport||{day:'2026-11-12',groups:[]}};
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
  if (scenario.automaticRetry) {
    const card=node('theta-automatic-results').children[0];
    card.children.find(child=>child.tag==='button').listeners.click();
    await settle(); await settle();
  }
  if (scenario.pairing) {
    node('theta-pairing-create').listeners.click();
    await settle(); await settle();
  }
  if (scenario.devicePairing) {node('theta-device-pairing-create').listeners.click();await settle();await settle();}
  if (scenario.revoke) {node('theta-devices').children[0].children.find(child=>child.tag==='button').listeners.click();await settle();await settle();}
  if (scenario.rerun) {node('theta-rerun').listeners.click();await settle();await settle();}
  if (scenario.selectedSource) {node('theta-source').value=scenario.selectedSource;node('theta-source').listeners.change();}
  if (scenario.openQuote) {const d=node('theta-results').children[0].children.find(c=>c.tag==='details');d.open=true;d.listeners.toggle();}
  if (scenario.poll) {intervals[0].fn();await settle();await settle();}
  console.log(JSON.stringify({text:Object.fromEntries(Object.entries(nodes).map(([k,v])=>[k,v.textContent])),calls,connection:connection.dataset,navigations,intervals:intervals.map(row=>row.ms),selectedSource:node('theta-source').value,sourceSubmitDisabled:node('theta-source-submit').disabled,openQuotes:node('theta-results').children.flatMap(c=>c.children.filter(d=>d.tag==='details'&&d.open)).length}));
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


def device(**changes: object) -> dict[str, object]:
    return {
        "device_id": "thetadev_" + "a" * 32, "state": "idle", "bridge_online": True,
        "terminal_state": "reachable", "created_at": "2026-11-12T15:00:00Z",
        "last_heartbeat_at": "2026-11-12T15:30:00Z", "last_validated_data_at": None,
        "revoked_at": None, **changes,
    }


def test_empty_queue_device_enrollment_needs_no_manual_job_and_hides_capability() -> None:
    token = "b" * 64
    value = run_ui({"status": {"jobs": [], "recent_results": []},
                    "devicePairing": {"pairing_token": token, "expires_at": "2026-11-13T15:00:00Z"}})
    posts = [row for row in value["calls"] if row["options"]["method"] == "POST"]
    assert [row["url"] for row in posts] == ["/api/theta/devices/pairings"]
    assert json.loads(posts[0]["options"]["body"]) == {}
    assert value["navigations"] == ["/theta-otomatik#" + token]
    assert token not in json.dumps(value["text"])


def test_idle_heartbeat_is_shown_without_claiming_validated_quote_data() -> None:
    value = run_ui({"status": {"jobs": []}, "devices": {"status": "idle", "devices": [device()]}})
    assert value["text"]["theta-connection-title"] == "Program bağlı, ölçüm bekliyor"
    assert "Henüz gerçek bir giriş kotasyonu doğrulanmadı" in value["text"]["theta-connection-detail"]
    assert "Bilinmiyor" in value["text"]["theta-device-evidence"]


def test_stale_device_never_reuses_old_reachable_or_quote_as_live_status() -> None:
    value = run_ui({"status": {"status": "data_received", "jobs": []}, "devices": {
        "status": "stale", "devices": [device(state="stale", bridge_online=False,
                                                last_validated_data_at="2026-11-11T15:00:00Z")],
    }})
    assert value["connection"]["connectionState"] == "error"
    assert "güncel yanıt alınamıyor" in value["text"]["theta-connection-title"]
    assert "şimdi çalıştığını kanıtlamaz" in value["text"]["theta-connection-detail"]


def test_device_revocation_is_explicit_and_scoped_to_its_identifier() -> None:
    scenario = {"status": {"jobs": []}, "devices": {"status": "idle", "devices": [device()]}}
    assert not any(row["options"]["method"] == "POST" for row in run_ui(scenario)["calls"])
    value = run_ui({**scenario, "revoke": True})
    posts = [row for row in value["calls"] if row["options"]["method"] == "POST"]
    assert [row["url"] for row in posts] == ["/api/theta/devices/thetadev_" + "a" * 32 + "/revoke"]
    assert json.loads(posts[0]["options"]["body"]) == {}


def test_automatic_admission_and_exclusion_keep_complete_scope_separate_from_manual() -> None:
    automatic_job = {**job(), "source_kind": "opening_entry"}
    value = run_ui({"status": {"jobs": [automatic_job], "recent_results": []}, "automatic": {
        "status": "ready", "day": "2026-11-12", "counts": {"observed": 2, "admitted": 1,
        "excluded": 1, "pending": 1, "evaluated": 0, "unknown": 0, "pending_maturity": 1,
        "awaiting_connector": 0}, "recent": [
            {**automatic_job, "disposition": "admitted", "state": "pending_maturity",
             "entry_basis": "observed_committed_source_plus_60_seconds"},
            {"ticker": "META", "disposition": "excluded", "reason": "multiple_legs",
             "entry_basis": "retrospective_publication_plus_60_seconds"},
        ], "latest_report": None,
    }})
    text = value["text"]["theta-automatic-results"]
    assert "Otomatik kayıtlı giriş" in text and "Bekliyor" in text
    assert "birden fazla opsiyon bacağı" in text
    assert "Yeni gözlenen kayıtlar" in text and "Geçmişten eklenen kayıtlar" in text
    assert "TSLA261127C00410000" not in value["text"]["theta-results"]


def test_status_polling_is_every_fifteen_seconds_and_never_creates_requests() -> None:
    value = run_ui({"status": {"jobs": []}, "poll": True})
    assert value["intervals"] == [15000]
    assert len(value["calls"]) == 8
    assert all(row["options"]["method"] == "GET" for row in value["calls"])


def test_stored_data_rerun_uses_selected_day_and_no_provider_or_manual_job_endpoint() -> None:
    value = run_ui({"status": {"jobs": []}, "automatic": {
        "status": "ready", "day": "2026-11-11", "counts": {}, "recent": [],
    }, "rerun": True, "rerunReport": {"day": "2026-11-11", "groups": []}})
    posts = [row for row in value["calls"] if row["options"]["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["url"] == "/api/theta/automatic/reruns"
    assert json.loads(posts[0]["options"]["body"]) == {"day": "2026-11-11"}


def test_retained_upload_is_not_hidden_by_reachable_terminal() -> None:
    value = run_ui({"status": {"jobs": []}, "devices": {"status": "online", "devices": [
        device(state="online", last_error="upload_failed", outbox_count=1),
    ]}})
    assert value["connection"]["connectionState"] == "error"
    assert "Son veri aktarımı kabul edilmedi" in value["text"]["theta-connection-detail"]


@pytest.mark.parametrize("state", ["evaluated", "unknown"])
def test_old_automatic_result_without_recent_receipt_never_becomes_pending(state: str) -> None:
    value = run_ui({"status": {"jobs": [], "recent_results": []}, "automatic": {
        "status": "ready", "day": "2026-11-11", "counts": {"retry_limit": 2}, "recent": [
            {**job(), "state": state, "disposition": "admitted",
             "entry_basis": "retrospective_publication_plus_60_seconds"},
        ],
    }})
    text = value["text"]["theta-automatic-results"]
    assert "Ayrıntı yok" in text and "Günlük ölçüm özetine" in text
    assert "Bekliyor" not in text and "Kotasyon bekleniyor" not in text
    assert "Yeniden deneme gerekli 2" in value["text"]["theta-automatic-counts"]


def test_poll_preserves_selected_historical_source_and_open_quote_detail() -> None:
    value = run_ui({"status": {"jobs": [job()], "recent_results": [{
        "request_id": "theta_test", "result": {"source": "thetadata", "status": "PARTIAL",
        "entry_quote": {"bid": "1", "ask": "1.2"}, "markouts": []},
    }]}, "sources": [{"source_kind": "decision_card", "source_id": "saved-1",
                      "option_symbol": "TSLA261127C00410000"}],
        "selectedSource": "saved-1", "openQuote": True, "poll": True})
    assert value["selectedSource"] == "saved-1"
    assert value["sourceSubmitDisabled"] is False
    assert value["openQuotes"] == 1


def test_report_keeps_prospective_and_retrospective_groups_and_unknown_net_separate() -> None:
    value = run_ui({"status": {"jobs": []}, "automatic": {
        "status": "ready", "day": "2026-11-11", "counts": {}, "recent": [], "latest_report": {
            "day": "2026-11-11", "groups": [{
                "entry_basis": "observed_committed_source_plus_60_seconds",
                "counts": {"admitted": 2, "excluded": 1}, "horizons": [{
                    "minutes": 15, "evaluated": 0, "unknown": 1, "pending": 1,
                    "positive": 0, "negative": 0, "zero": 0, "modeled_net_pnl_usd_total": "0",
                }],
            }, {
                "entry_basis": "retrospective_publication_plus_60_seconds",
                "counts": {"admitted": 1, "excluded": 0}, "horizons": [{
                    "minutes": 60, "evaluated": 1, "unknown": 0, "pending": 0,
                    "positive": 0, "negative": 1, "zero": 0, "modeled_net_pnl_usd_total": "-88.30",
                }],
            }],
        },
    }})
    text = value["text"]["theta-automatic-report"]
    assert "Yeni gözlenen kayıtlar" in text and "Geçmişten eklenen kayıtlar" in text
    assert "Bilinmiyor" in text and "88,30" in text and "0,00" not in text
    assert "portföy getirisi değildir" in text and "alternatiflerdir, toplanmaz" in text


@pytest.mark.parametrize("state, phrase", [
    ("awaiting_observer", "İlk otomatik kontrol bekleniyor"),
    ("stale", "Otomatik kontrol gecikti"),
    ("failed", "Otomatik kontrol tamamlanamadı"),
    ("no_source_storage", "Giriş kayıtları okunamadı"),
])
def test_observer_unverified_states_are_not_presented_as_healthy(state: str, phrase: str) -> None:
    value = run_ui({"status": {"jobs": []}, "automatic": {
        "status": state, "day": "2026-11-11", "counts": {}, "recent": [],
    }})
    assert value["text"]["theta-automatic-title"] == phrase


def test_automatic_retry_limit_cannot_create_legacy_retry_outside_frozen_cohort() -> None:
    value = run_ui({"status": {"jobs": [], "recent_results": []}, "automatic": {
        "status": "ready", "day": "2026-11-11", "counts": {"retry_limit": 1}, "recent": [
            {**job(), "state": "retry_limit", "disposition": "admitted", "source_kind": "opening_entry",
             "entry_basis": "observed_committed_source_plus_60_seconds"},
        ],
    }})
    text = value["text"]["theta-automatic-results"]
    assert "deneme sınırına ulaşıldı" in text
    assert "Bağlantıyı düzelttim, yeniden dene" not in text
    assert all(call["options"]["method"] == "GET" for call in value["calls"])


def test_unobserved_source_backlog_is_not_implicitly_counted_as_complete_cohort() -> None:
    value = run_ui({"status": {"jobs": []}, "automatic": {
        "status": "ready", "day": "2026-11-11", "counts": {"observed": 32}, "recent": [],
        "source_context": {"unobserved": 256, "unobserved_is_lower_bound": True,
                           "scan_complete": False, "denominator": "observed_immutable_admissions"},
    }})
    text = value["text"]["theta-automatic-detail"]
    assert "İşlenmemiş kaynak kaydı: en az 256" in text
    assert "yalnızca kaydedilmiş girişleri" in text and "henüz tamamlanmadı" in text


def test_unavailable_storage_never_turns_placeholder_counts_into_observed_zeros() -> None:
    value = run_ui({"status": {"jobs": []}, "automatic": {
        "status": "storage_unavailable", "day": "2026-11-11", "counts": {
            "observed": 0, "admitted": 0, "evaluated": 0, "pending": 0,
            "unknown": 0, "retry_limit": 0, "excluded": 0,
            "pending_maturity": 0, "awaiting_connector": 0,
        }, "recent": [],
    }})
    tiles = value["text"]["theta-automatic-counts"]
    assert tiles.count("Bilinmiyor") == 7
    assert "0" not in tiles
    detail = value["text"]["theta-automatic-detail"]
    assert "Zamanı beklenen" not in detail and "Kotasyonu beklenen" not in detail


def test_automatic_explicit_retry_uses_active_descendant_and_keeps_source_target() -> None:
    scenario = {"status": {"jobs": [], "recent_results": []}, "automatic": {
        "status": "ready", "day": "2026-11-11", "counts": {"retry_limit": 1}, "recent": [
            {**job(), "request_id": "theta_active_child", "original_request_id": "theta_original",
             "state": "retry_limit", "disposition": "admitted", "source_kind": "opening_entry",
             "retry_available": True, "retry_depth": 2,
             "entry_basis": "observed_committed_source_plus_60_seconds"},
        ],
    }}
    initial = run_ui(scenario)
    assert "Aynı kayıt için yeniden başlatma: 2" in initial["text"]["theta-automatic-results"]
    assert all(call["options"]["method"] == "GET" for call in initial["calls"])
    value = run_ui({**scenario, "automaticRetry": True})
    posts = [call for call in value["calls"] if call["options"]["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["url"] == "/api/theta/requests/theta_active_child/retry"
    assert json.loads(posts[0]["options"]["body"]) == {}
    assert "Yeni deneme kaydedildi" in value["text"]["theta-device-message"]
