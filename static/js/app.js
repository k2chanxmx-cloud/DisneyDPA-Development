const $ = (id) => document.getElementById(id);
const form = $('forecastForm');
const today = new Date();
$('dateInput').value = today.toISOString().slice(0,10);

const esc = (v) => String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const yen = (v) => v == null ? '価格未取得' : `${Number(v).toLocaleString('ja-JP')}円`;

async function loadStatus(){
  try{
    const r = await fetch('/api/status'); const d = await r.json();
    $('statusBadge').textContent = `${d.environment || 'unknown'} / ${d.supabase_connected ? 'DB接続済み' : 'デモモード'}`;
    $('statusBadge').className = `status-badge ${d.supabase_connected ? 'ok':'warn'}`;
    $('versionText').textContent = `Version ${d.version} · ${d.build}`;
  }catch{ $('statusBadge').textContent='状態取得失敗'; $('statusBadge').className='status-badge warn'; }
}

form.addEventListener('submit', async (e)=>{
  e.preventDefault();
  $('loading').classList.remove('hidden'); $('errorBox').classList.add('hidden'); $('result').classList.add('hidden');
  $('submitButton').disabled = true;
  try{
    const q = new URLSearchParams({date:$('dateInput').value, entry_time:$('timeInput').value});
    const r = await fetch(`/api/forecast?${q}`); const d = await r.json();
    if(!r.ok || d.error) throw new Error(d.error_detail || d.error || `HTTP ${r.status}`);
    render(d);
  }catch(err){ $('errorBox').textContent=`予測できませんでした：${err.message}`; $('errorBox').classList.remove('hidden'); }
  finally{ $('loading').classList.add('hidden'); $('submitButton').disabled=false; }
});

function render(d){
  $('crowdLabel').textContent=d.crowd_label ?? '-'; $('crowdScore').textContent=d.crowd_score==null?'スコア未取得':`混雑スコア ${d.crowd_score}`;
  $('weather').textContent=d.weather ?? '-'; $('temperature').textContent=(d.temperature_high==null&&d.temperature_low==null)?'気温未取得':`${d.temperature_low ?? '-'}℃ ～ ${d.temperature_high ?? '-'}℃`;
  $('openingHours').textContent=`${d.official_open_time ?? '-'} ～ ${d.official_close_time ?? '-'}`; $('ticketPrice').textContent=yen(d.ticket_price);
  const c=d.prediction_confidence||{}; $('confidence').textContent=c.score==null?'-':`${c.score}%`; $('confidenceStars').textContent=`${c.stars_text ?? ''} ${c.label ?? ''}`;
  $('predictionMeta').textContent=`実績 ${d.history_count ?? 0}件 / 使用 ${d.sample_count ?? 0}件`;
  $('attractionCards').innerHTML=(d.attractions||[]).map(a=>`<article class="attraction"><h3>${esc(a.name)}</h3><div class="probability">${esc(a.acquisition_probability)}%</div><div>入園時刻での取得予測</div><div class="progress"><i style="width:${Math.max(0,Math.min(100,Number(a.acquisition_probability)||0))}%"></i></div><div class="sellout">予測売切れ <strong>${esc(a.predicted_sellout_time||'未算出')}</strong><div class="range">範囲 ${esc(a.confidence_low||'-')} ～ ${esc(a.confidence_high||'-')} / ${esc(a.sample_count||0)}件</div></div></article>`).join('');
  $('reasons').innerHTML=(d.reasons||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  const sd=d.source_diagnostics||{};
  const sources=[['Supabase',sd.supabase],['公式カレンダー',sd.official_calendar],['Yosocalカレンダー',sd.yosocal_calendar],['Yosocal天気',sd.yosocal_weather]];
  $('sourceInfo').innerHTML=sources.map(([n,s])=>`<div class="source-item"><span>${n}</span><b class="${s?.success?'ok':'ng'}">${s?.success?'成功':'未取得'}</b></div>`).join('')+`<div class="source-item"><span>予測ログ保存</span><b class="${d.prediction_saved?'ok':'ng'}">${d.prediction_saved?'成功':'失敗'}</b></div>`;
  $('factorCount').textContent=`(${d.yosocal_factor_count||0}件)`; $('closureCount').textContent=`(${d.yosocal_closure_count||0}件)`;
  $('factors').innerHTML=(d.yosocal_factors||[]).map(x=>`<div class="mini-row"><b>${esc(x.name)}</b><small>${esc(x.type_label)} / ${esc(x.start_date)}〜${esc(x.end_date)}</small></div>`).join('')||'<p>対象データなし</p>';
  $('closures').innerHTML=(d.yosocal_closures||[]).map(x=>`<div class="mini-row"><b>${esc(x.name)}</b><small>${esc((x.park||'').toUpperCase())} / ${esc(x.start_date)}〜${esc(x.end_date)}</small></div>`).join('')||'<p>対象データなし</p>';
  $('rawJson').textContent=JSON.stringify(d,null,2); $('result').classList.remove('hidden');
}
loadStatus();
