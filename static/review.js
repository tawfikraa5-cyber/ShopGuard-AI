// A stable review queue: polling must never restart the clip being watched.
let selectedReviewId = null;
const reviewDrafts = new Map();
let undoStack = [];
try {
  undoStack = JSON.parse(sessionStorage.getItem('shopguard-undo') || '[]');
  if (!Array.isArray(undoStack)) undoStack = [];
} catch {}
let reviewRate = 1;
function undoForCurrentRun() {
  if (state.file) return null;
  return undoStack
    .filter((item) => item.run_id === (state.mode === 'demo' ? 'demo' : state.jobId))
    .at(-1);
}
function rememberReview(token, event) {
  if (!token || !event) return;
  reviewDrafts.delete(event.id);
  undoStack.push({ token, event_id: event.id, run_id: event.run_id });
  undoStack = undoStack.slice(-30);
  sessionStorage.setItem('shopguard-undo', JSON.stringify(undoStack));
  updateUndo();
}
function updateUndo() {
  const button = $('undo-review');
  button.hidden = !undoForCurrentRun();
}
async function undoReview() {
  const item = undoForCurrentRun();
  if (!item) return;
  const button = $('undo-review');
  button.disabled = true;
  try {
    await api('/api/review/undo', { review_id: item.token });
    undoStack = undoStack.filter((other) => other.token !== item.token);
    sessionStorage.setItem('shopguard-undo', JSON.stringify(undoStack));
    selectedReviewId = item.event_id;
    await refresh();
    $('alerts-content').dataset.reviewKey = '';
    renderAlerts();
    renderCamera();
    toast('تراجعنا عن القرار، والحدث رجع للمراجعة.');
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    updateUndo();
  }
}
document.getElementById('undo-review').onclick = undoReview;
function pendingReviews() {
  return state.events
    .filter(
      (e) =>
        e.ai &&
        e.status === 'pending' &&
        (!state.jobId || state.mode === 'demo' || e.run_id === state.jobId),
    )
    .sort((a, b) => reviewPriority(b) - reviewPriority(a));
}
function currentReview() {
  if (state.file) return null;
  const pending = pendingReviews();
  return pending.find((e) => e.id === selectedReviewId) || pending[0] || null;
}
function reviewFacts(event) {
  try {
    return JSON.parse(event.evidence || '{}');
  } catch {
    return {};
  }
}
function reviewVideoSource(event) {
  const facts = reviewFacts(event);
  return typeof facts.source === 'string' && facts.source.startsWith('/media/uploads/')
    ? facts.source
    : null;
}
function videoTime(value) {
  const second = Math.max(0, Number(value) || 0);
  return (
    String(Math.floor(second / 60)).padStart(2, '0') +
    ':' +
    String(Math.floor(second % 60)).padStart(2, '0')
  );
}
function renderReviewVideo(event) {
  const stage = $('camera-stage'),
    facts = reviewFacts(event),
    source = reviewVideoSource(event);
  const expanded = $('review-context').checked;
  const trigger = Number(facts.trigger_second ?? event.video_second) || 0;
  const start = expanded ? Math.max(0, trigger - 15) : Math.max(0, Number(facts.clip_start) || 0),
    end = expanded ? trigger + 15 : Number(facts.clip_end);
  const existing = $('review-video');
  const key = event.id + '|' + expanded;
  if (!existing || existing.dataset.window !== key) {
    stage.innerHTML = `<video id="review-video" data-event="${esc(event.id)}" data-window="${esc(key)}" data-start="${start}" data-end="${Number.isFinite(end) ? end : ''}" src="${esc(source)}#t=${start}${Number.isFinite(end) ? ',' + end : ''}" controls autoplay muted playsinline preload="metadata" aria-label="مقطع التنبيه للمراجعة"></video>`;
    const video = $('review-video');
    video.playbackRate = reviewRate;
    video.onloadedmetadata = () => {
      if (video.videoWidth) stage.style.aspectRatio = `${video.videoWidth}/${video.videoHeight}`;
    };
  }
  $('review-tools').hidden = false;
  const fps = Number(facts.source_fps || state.job?.source_fps);
  $('frame-help').textContent =
    fps > 0
      ? `الخطوة = إطار واحد حسب توقيت المصدر (${fps.toFixed(2)} FPS).`
      : 'توقيت الإطارات ما متوفرش فهاد التحليل القديم؛ الخطوة تقريبية (1/30 ثانية).';
  $('source-name').textContent =
    'مقطع التنبيه · ' + videoTime(start) + (Number.isFinite(end) ? ' — ' + videoTime(end) : '');
  $('camera-status').textContent = 'مراجعة الحدث';
}
document.getElementById('review-rate').onchange = (event) => {
  reviewRate = Number(event.target.value);
  const video = $('review-video');
  if (video) video.playbackRate = reviewRate;
};
document.getElementById('review-context').onchange = () => {
  const event = currentReview();
  if (event && reviewVideoSource(event)) renderReviewVideo(event);
};
document.getElementById('review-tools').onclick = (event) => {
  const action = event.target.closest('[data-player]')?.dataset.player;
  const video = $('review-video');
  if (!action || !video || !video.readyState) return;
  const start = Number(video.dataset.start) || 0;
  const end = Math.min(Number(video.dataset.end) || video.duration, video.duration);
  if (action === 'replay') {
    video.currentTime = start;
    video.play().catch(() => toast('ضغط على تشغيل الفيديو باش يعاود.'));
    return;
  }
  const facts = reviewFacts(currentReview() || {});
  const fps = Number(facts.source_fps || state.job?.source_fps) || 30;
  video.pause();
  video.currentTime = Math.max(
    start,
    Math.min(end, video.currentTime + (action === 'back' ? -1 : 1) / fps),
  );
};
function renderAlerts() {
  if (state.file) {
    $('pending-count').textContent = '—';
    $('undo-review').hidden = true;
    $('alerts-content').dataset.reviewKey = 'file|' + state.file.name;
    $('alerts-content').innerHTML =
      '<div class="empty-alerts"><strong>الفيديو باقي ما تحلّلش</strong><p>حدّد المنطقة وضغط على بدا التحليل باش يبانُو التنبيهات ديالو.</p></div>';
    return;
  }
  const pending = pendingReviews(),
    active = currentReview();
  const reviewed = state.events.filter(
    (e) =>
      e.ai &&
      e.status !== 'pending' &&
      (!state.jobId || state.mode === 'demo' || e.run_id === state.jobId),
  );
  $('pending-count').textContent = pending.length;
  updateUndo();
  const position = $('alerts-content').querySelector('.queue-position');
  if (position) position.textContent = `${pending.length} تنبيهات باقين · كنراجعو واحد بواحد`;
  const key =
    (active ? active.id : 'empty') +
    '|' +
    state.jobId +
    '|' +
    (active ? '' : reviewed.length) +
    '|' +
    (state.file?.name || '');
  if ($('alerts-content').dataset.reviewKey === key) return;
  $('alerts-content').dataset.reviewKey = key;
  if (active) {
    selectedReviewId = active.id;
    const facts = reviewFacts(active);
    const second = facts.trigger_second ?? active.video_second;
    $('alerts-content').innerHTML =
      `<article class="alert-item focused-review"><div class="queue-position">${pending.length} تنبيهات باقين · كنراجعو واحد بواحد</div><div class="alert-title"><span class="alert-icon">${icon('alert')}</span><div><h3>${eventTitle(active)}</h3><span>${facts.live ? 'فجلسة الكاميرا' : 'فالفيديو'} <b dir="ltr">${videoTime(second)}</b></span></div></div>${!reviewVideoSource(active) && active.snapshot ? `<img class="event-snapshot" src="${esc(active.snapshot)}" alt="صورة الحدث">` : ''}${eventEvidence(active).replace(/<details class="event-clip">[\s\S]*?<\/details>/, '')}<p class="review-help">شوف المقطع، ومن بعد سجّل القرار. إلا ما واضحش، دوز للتنبيه الموالي وخليه معلّق.</p><details class="review-note"><summary>زيد ملاحظة للقرار (اختياري)</summary><textarea id="review-note" rows="2" maxlength="1000" aria-label="ملاحظة القرار">${esc(reviewDrafts.get(active.id) ?? active.note ?? '')}</textarea></details><div class="review-actions"><button class="button small" data-review="${esc(active.id)}" data-status="confirmed">الحدث صحيح <kbd>1</kbd></button><button class="button small quiet" data-review="${esc(active.id)}" data-status="false_positive">تنبيه خاطئ <kbd>2</kbd></button><button class="skip-review" data-skip="true">مازال ما واضحش ←</button></div></article>`;
    if (reviewVideoSource(active)) renderReviewVideo(active);
  } else {
    $('alerts-content').innerHTML =
      `<div class="empty-alerts">${icon('check')}<strong>${state.job?.status === 'running' ? 'كنقلبو على أحداث للمراجعة…' : 'المراجعة واجدة'}</strong><p>${state.job?.status === 'running' ? 'أي تنبيه جديد غادي يبان هنا.' : 'ما بقى حتى تنبيه معلّق فهاد الفيديو.'}</p></div>`;
    selectedReviewId = null;
  }
  const exported =
    '/api/export?mode=' +
    state.mode +
    (state.jobId ? '&run_id=' + encodeURIComponent(state.jobId) : '');
  if (reviewed.length)
    $('alerts-content').insertAdjacentHTML(
      'beforeend',
      `<details class="reviewed"><summary>أحداث تراجعات (${reviewed.length})</summary>${reviewed.map((e) => `<div><span>تتبع #${e.track_id} · ${videoTime(reviewFacts(e).trigger_second ?? e.video_second)}</span><span class="${e.status === 'confirmed' ? 'review-correct' : ''}">${e.status === 'confirmed' ? 'تراجع · صحيح' : 'تنبيه خاطئ'}</span></div>`).join('')}<a class="export-link" href="${exported}" download>تحميل سجل الأحداث CSV ↓</a></details>`,
    );
}
function skipReview() {
  const pending = pendingReviews(),
    current = currentReview();
  if (pending.length < 2) {
    toast('هاد آخر تنبيه. خليه معلّق إلا ما واضحش.');
    return;
  }
  selectedReviewId =
    pending[(pending.findIndex((e) => e.id === current?.id) + 1) % pending.length].id;
  $('alerts-content').dataset.reviewKey = '';
  renderAlerts();
  renderCamera();
}
document.addEventListener('keydown', (event) => {
  if (
    event.repeat ||
    event.ctrlKey ||
    event.metaKey ||
    event.altKey ||
    event.target.closest('input,select,textarea,[contenteditable="true"]')
  )
    return;
  if (!['1', '2'].includes(event.key) || state.file) return;
  const button = $('alerts-content').querySelector(
    `[data-status="${event.key === '1' ? 'confirmed' : 'false_positive'}"]`,
  );
  if (button && !button.disabled) {
    event.preventDefault();
    button.click();
  }
});
document.addEventListener('input', (event) => {
  if (event.target.id === 'review-note' && selectedReviewId)
    reviewDrafts.set(selectedReviewId, event.target.value);
});
document.addEventListener('keydown', (event) => {
  if (
    (event.ctrlKey || event.metaKey) &&
    event.key.toLowerCase() === 'z' &&
    !event.target.closest('input,textarea,[contenteditable="true"]') &&
    undoForCurrentRun()
  ) {
    event.preventDefault();
    undoReview();
  }
});
