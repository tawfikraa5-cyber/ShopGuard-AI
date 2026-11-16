const paths = {
  upload: 'M12 16V3M7 8l5-5 5 5M4 15v6h16v-6',
  play: 'm7 3 14 9-14 9z',
  target: 'M12 2v4M12 18v4M2 12h4M18 12h4M19 12a7 7 0 1 1-14 0 7 7 0 0 1 14 0',
  shield: 'M12 2 3 6v6c0 6 9 10 9 10s9-4 9-10V6zM8 12l3 3 5-6',
  alert: 'M12 3 1 21h22zM12 9v5M12 17v1',
  check: 'M5 12l4 4L20 5',
  video: 'M3 5h12v14H3zM15 9l6-4v14l-6-4',
  users: 'M16 21v-3a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v3M13 5a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.video}"/></svg>`;
const esc = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
const $ = (id) => document.getElementById(id);
const state = {
  events: [],
  settings: {},
  mode: 'demo',
  job: null,
  jobId: null,
  file: null,
  objectUrl: null,
  zone: null,
  busy: false,
};
let toastTimer;
function reviewPriority(event) {
  try {
    const reasons = JSON.parse(event.reasons || '[]');
    return reasons.includes('hand_return') ? 3 : reasons.includes('reentry') ? 2 : 1;
  } catch {
    return 1;
  }
}
function eventTitle(event) {
  const priority = reviewPriority(event);
  return priority === 3
    ? 'حركة اليد خاصّها المراجعة'
    : priority === 2
      ? 'رجوع متكرر للمنطقة'
      : 'بقاء طويل فالمنطقة';
}
async function loadRuns() {
  try {
    const runs = await api('/api/runs');
    const select = $('saved-runs');
    select.hidden = !runs.length;
    select.innerHTML = runs
      .map(
        (r) =>
          `<option value="${esc(r.id)}">${esc(r.source_name)}${r.dwell_seconds ? ' · ' + r.dwell_seconds + 'ث' : ''}${r.engine_version === 'activity-cues-v3' ? ' · محسّن' : ''}${r.status === 'running' ? ' · خدام' : ''}</option>`,
      )
      .join('');
    if (state.jobId) select.value = state.jobId;
    else if (runs.length && !state.file) {
      state.jobId = runs[0].id;
      state.job = await api('/api/jobs/' + state.jobId);
      state.mode = 'real';
      sessionStorage.setItem('shopguard-job', state.jobId);
      await refresh();
    }
  } catch {}
}
$('saved-runs').onchange = async (e) => {
  try {
    state.file = null;
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = null;
    state.jobId = e.target.value;
    state.job = await api('/api/jobs/' + state.jobId);
    sessionStorage.setItem('shopguard-job', state.jobId);
    state.mode = 'real';
    await refresh();
    renderCamera();
    renderProgress();
    renderAlerts();
  } catch (err) {
    toast(err.message, true);
  }
};
function eventEvidence(event) {
  let facts = {},
    reasons = [];
  try {
    facts = JSON.parse(event.evidence || '{}');
    reasons = JSON.parse(event.reasons || '[]');
  } catch {}
  const labels = {
    dwell: `بقاء متواصل فالمنطقة: ${Math.round(event.duration)} ثانية`,
    reentry: `رجوع متكرر للمنطقة: ${facts.entries || 3} مرات`,
    hand_return: 'حركة اليد من المنطقة نحو الجسم تكررات؛ خاص التحقق بالفيديو',
  };
  if (!reasons.length) reasons = ['dwell'];
  const source =
    typeof facts.source === 'string' && facts.source.startsWith('/media/uploads/')
      ? facts.source
      : null;
  const start = Number(facts.clip_start),
    end = Number(facts.clip_end);
  return `<p>تتبع #${event.track_id} · ${esc(event.zone)}</p><ul class="evidence-list">${reasons.map((reason) => `<li>${esc(labels[reason] || 'حركة للمراجعة')}</li>`).join('')}</ul>${Number.isFinite(facts.confidence) ? `<p class="confidence-note">ثقة كشف الشخص ${Math.round(facts.confidence * 100)}%، ماشي احتمال السرقة.</p>` : ''}${source && Number.isFinite(start) && Number.isFinite(end) ? `<details class="event-clip"><summary>شوف المقطع من ${start.toFixed(1)} ثانية</summary><video controls playsinline preload="none" src="${esc(source)}#t=${start},${end}" aria-label="مقطع الحدث للمراجعة"></video></details>` : ''}`;
}
function toast(message, error = false) {
  $('toast').textContent = message;
  $('toast').className = 'toast show' + (error ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').classList.remove('show'), 4500);
}
function uploadVideo(file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    state.uploadRequest = request;
    request.open('POST', '/api/upload');
    request.responseType = 'json';
    request.setRequestHeader('Content-Type', 'application/octet-stream');
    request.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    $('progress-section').hidden = false;
    $('quality-note').textContent = 'الفيديو كيمشي غير للسيرفر المحلي فالحاسوب ديالك.';
    $('cancel-analysis').hidden = false;
    $('cancel-analysis').disabled = false;
    $('cancel-analysis').textContent = 'وقف رفع الفيديو';
    request.upload.onprogress = (event) => {
      const percent = event.lengthComputable ? Math.round((event.loaded / event.total) * 100) : 0;
      $('job-message').textContent = 'كنرفعو الفيديو…';
      $('job-percent').textContent = percent + '%';
      $('job-progress').value = percent;
    };
    request.onload = () => {
      state.uploadRequest = null;
      request.status >= 200 && request.status < 300
        ? resolve(request.response)
        : reject(Error(request.response?.error || 'رفع الفيديو ما كملش. عاود جرّب.'));
    };
    request.onerror = () => {
      state.uploadRequest = null;
      reject(Error('الاتصال تقطع أثناء رفع الفيديو.'));
    };
    request.onabort = () => {
      state.uploadRequest = null;
      reject(Error('توقف رفع الفيديو.'));
    };
    request.send(file);
  });
}
$('cancel-analysis').onclick = async () => {
  if (state.uploadRequest) {
    state.uploadRequest.abort();
    return;
  }
  if (!state.jobId || state.job?.status !== 'running') return;
  const button = $('cancel-analysis');
  button.disabled = true;
  try {
    await api('/api/cancel', { id: state.jobId });
    state.job.cancel_requested = true;
    button.textContent = 'كنوقفو التحليل…';
    toast('طلب التوقيف تسجل. غادي تبقى النتائج اللي واجدة.');
  } catch (err) {
    button.disabled = false;
    toast(err.message, true);
  }
};
async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
  );
  const data = await response.json();
  if (!response.ok) {
    const error = Error(data.error || 'وقع مشكل فالاتصال. عاود جرّب.');
    error.status = response.status;
    throw error;
  }
  return data;
}
async function refresh() {
  const data = await api(
    `/api/state?mode=${state.mode}${state.jobId && state.mode === 'real' ? '&run_id=' + encodeURIComponent(state.jobId) : ''}`,
  );
  state.events = data.events;
  state.settings = data.settings;
}
function zoneStyle(zone) {
  return `left:${zone[0] * 100}%;top:${zone[1] * 100}%;width:${(zone[2] - zone[0]) * 100}%;height:${(zone[3] - zone[1]) * 100}%`;
}
function setBusy(busy) {
  state.busy = busy;
  $('choose-video').disabled = busy;
  $('saved-runs').disabled = busy;
  $('start-analysis').disabled = busy;
  $('dwell-seconds').disabled = busy;
  $('analysis-profile').disabled = busy;
}
function renderInfo() {
  const demo = state.mode === 'demo';
  $('mode-note').textContent = state.file
    ? 'الفيديو واجد. حدّد المنطقة ومدة البقاء، ومن بعد بدا التحليل.'
    : demo
      ? 'وضع تجريبي: المشهد والتنبيهات غير للتجربة. اختار فيديو ديالك باش تبدا.'
      : 'التنبيهات كتحتاج مراجعتك. الحركة بوحدها ما كتثبت حتى مخالفة.';
  $('people-count').innerHTML =
    icon('users') +
    ' ' +
    (demo && !state.file
      ? '3 أشخاص · تجريبي'
      : state.job && !state.file
        ? `${state.job.people ?? 0} أشخاص فآخر إطار`
        : 'كشف الأشخاص كيبدا مع التحليل');
  $('choose-video').innerHTML =
    (state.file || state.job ? 'بدّل الفيديو' : 'اختار فيديو') + ' ' + icon('upload');
}
function renderCamera() {
  $('review-tools').hidden = true;
  $('live-controls').hidden = true;
  if (liveCapture.id && liveCapture.stream) {
    renderLiveCamera();
    return;
  }
  const stage = $('camera-stage');
  const review = currentReview();
  if (!state.file && review && !reviewVideoSource(review) && review.snapshot) {
    $('camera-stage').innerHTML =
      '<img src="' + esc(review.snapshot) + '" alt="صورة التنبيه من الكاميرا">';
    $('camera-status').textContent = 'صورة التنبيه';
    $('source-name').textContent = 'الصورة اللي تسجلات فالتنبيه';
    $('video-setup').hidden = true;
    renderInfo();
    return;
  }
  if (!state.file && review && reviewVideoSource(review)) {
    renderReviewVideo(review);
    $('video-setup').hidden = true;
    renderInfo();
    return;
  }
  if (state.file) {
    stage.innerHTML = `<video id="selected-video" src="${state.objectUrl}" controls playsinline preload="metadata"></video><div class="zone-layer" id="zone-layer" aria-label="جرّ باش تحدّد المنطقة الحساسة"><div class="zone-overlay" id="zone-overlay" style="${zoneStyle(state.zone)}"><span>المنطقة الحساسة</span></div></div>`;
    const video = $('selected-video');
    video.onerror = () => {
      video.removeAttribute('controls');
      const hint = document.createElement('p');
      hint.className = 'preview-error';
      hint.textContent =
        'المتصفح ما قدرش يعرض المعاينة. استعمل MP4 باش تحدّد المنطقة بدقة، أو حلّل بالمنطقة الحالية.';
      stage.appendChild(hint);
    };
    video.onloadedmetadata = () => {
      if (video.videoWidth && video.videoHeight)
        stage.style.aspectRatio = `${video.videoWidth}/${video.videoHeight}`;
    };
    bindZone();
    $('source-name').textContent = state.file.name;
    $('camera-status').textContent = 'فيديو واجد للتحليل';
  } else if (state.mode === 'demo') {
    stage.style.aspectRatio = '16/9';
    stage.innerHTML = `<img src="/assets/store.svg" alt="مشهد تجريبي مرسوم لمحل تجاري"><div class="zone-overlay" style="${zoneStyle(state.settings.zone)}"><span>منطقة حساسة</span></div><div class="track-box" style="left:36.5%;top:55%;width:8.8%;height:25%"><span>شخص 02</span></div><div class="track-box warn" style="left:58.6%;top:43%;width:8%;height:25%"><span>شخص 03 · 42s</span></div><div class="track-box" style="left:48%;top:30%;width:6.5%;height:17%"><span>شخص 01</span></div><span class="demo-label">مشهد تجريبي</span>`;
    $('source-name').textContent = 'معاينة توضيحية للمحل';
    $('camera-status').textContent = 'تجريبي';
  } else if (state.job?.frame) {
    stage.innerHTML = `<img id="analysis-frame" src="${esc(state.job.frame)}?t=${Date.now()}" alt="الفيديو مع مربعات تتبع الأشخاص">`;
    $('analysis-frame').onload = (e) => {
      stage.style.aspectRatio = `${e.target.naturalWidth}/${e.target.naturalHeight}`;
    };
    $('source-name').textContent =
      (state.job.source_name || 'فيديو مسجّل') + ' · ' + (state.job.second ?? 0) + ' ثانية';
    $('camera-status').textContent =
      state.job.status === 'running'
        ? 'التحليل خدام'
        : state.job.status === 'complete'
          ? 'التحليل سالا'
          : 'التحليل توقف';
  } else {
    stage.innerHTML = `<div class="empty-camera">${icon('video')}<h3>${state.job?.status === 'running' ? 'كنوجدو التحليل…' : 'اختار فيديو باش تبدا'}</h3><p>MP4، MOV، AVI أو WebM · أقل من 150 MB</p></div>`;
    $('source-name').textContent = 'ما كاينش فيديو';
    $('camera-status').textContent =
      state.job?.status === 'running' ? 'كنوجدو النموذج' : 'فانتظار الفيديو';
  }
  $('video-setup').hidden = !state.file;
  renderInfo();
}
function renderProgress() {
  const job = state.job;
  $('progress-section').hidden = !job || !!state.file;
  if (!job) return;
  $('job-message').textContent = job.message;
  $('job-percent').textContent =
    job.live && job.status === 'running' ? 'مباشر' : (job.progress || 0) + '%';
  $('job-progress').hidden = !!job.live;
  $('job-progress').value = job.progress || 0;
  $('progress-section').classList.toggle('failed', ['error', 'interrupted'].includes(job.status));
  $('cancel-analysis').hidden = job.status !== 'running' || (job.live && !!liveCapture.id);
  $('cancel-analysis').disabled = !!job.cancel_requested;
  $('cancel-analysis').textContent = job.cancel_requested
    ? 'كنوقفو التحليل…'
    : job.live
      ? 'وقف جلسة الكاميرا'
      : 'وقف التحليل واحتافظ بالنتائج';
  $('quality-note').textContent = [
    ...(job.warnings || []),
    job.analysis_config
      ? 'عتبة البقاء فهاد التحليل: ' + job.analysis_config.dwell_seconds + ' ثواني.'
      : '',
    job.speed ? 'سرعة المعالجة: ' + job.speed + '× من مدة الفيديو.' : '',
  ]
    .filter(Boolean)
    .join(' ');
}
function bindZone() {
  const layer = $('zone-layer');
  let start = null;
  const point = (e) => {
    const r = layer.getBoundingClientRect();
    return [
      Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
      Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)),
    ];
  };
  const draw = (a, b) => {
    const zone = [
      Math.min(a[0], b[0]),
      Math.min(a[1], b[1]),
      Math.max(a[0], b[0]),
      Math.max(a[1], b[1]),
    ];
    if (zone[2] - zone[0] < 0.015 || zone[3] - zone[1] < 0.015) return;
    state.zone = zone;
    $('zone-overlay').style.cssText = zoneStyle(zone);
  };
  layer.onpointerdown = (e) => {
    start = point(e);
    layer.setPointerCapture(e.pointerId);
  };
  layer.onpointermove = (e) => {
    if (start) draw(start, point(e));
  };
  layer.onpointerup = (e) => {
    if (start) draw(start, point(e));
    start = null;
  };
  layer.onpointercancel = () => (start = null);
}
$('choose-video').onclick = () => $('video-input').click();
$('video-input').onchange = (e) => {
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
  if (
    !/\.(mp4|mov|avi|webm)$/i.test(file.name) ||
    file.size === 0 ||
    file.size > 150 * 1024 * 1024
  ) {
    toast('اختار فيديو مدعوم أصغر من 150 MB.', true);
    return;
  }
  if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
  state.file = file;
  state.objectUrl = URL.createObjectURL(file);
  state.zone = [...state.settings.zone];
  $('dwell-seconds').value = state.settings.dwell_seconds;
  $('analysis-profile').value = state.settings.analysis_profile || 'balanced';
  renderCamera();
  renderProgress();
  renderAlerts();
};
$('start-analysis').onclick = async () => {
  if (!state.file || state.busy) return;
  const seconds = Number($('dwell-seconds').value);
  if (!Number.isInteger(seconds) || seconds < 1 || seconds > 300) {
    toast('مدة البقاء خاصها تكون بين ثانية و300 ثانية.', true);
    $('dwell-seconds').focus();
    return;
  }
  setBusy(true);
  const button = $('start-analysis');
  button.textContent = 'كنرفعو الفيديو…';
  try {
    const health = await api('/api/health');
    if (!health.video_ready) throw Error('خاص تثبت YOLO وOpenCV الأول. الطريقة فـ README.');
    await api('/api/settings', {
      zone: state.zone,
      dwell_seconds: seconds,
      analysis_profile: $('analysis-profile').value,
    });
    const upload = await uploadVideo(state.file);
    const job = await api('/api/analyze', {
      filename: upload.filename,
      source_name: state.file.name,
      analysis_options: {
        zone: state.zone,
        dwell_seconds: seconds,
        analysis_profile: $('analysis-profile').value,
      },
    });
    state.jobId = job.id;
    sessionStorage.setItem('shopguard-job', job.id);
    state.job = { status: 'running', progress: 0, people: 0, message: 'التحليل غادي يبدا…' };
    state.file = null;
    URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = null;
    state.mode = 'real';
    await api('/api/settings', { demo: false });
    await refresh();
    renderCamera();
    renderProgress();
    renderAlerts();
    pollJob();
  } catch (err) {
    setBusy(false);
    renderProgress();
    toast(err.message, true);
  } finally {
    button.innerHTML = 'بدا التحليل ' + icon('play');
  }
};
$('alerts-content').onclick = async (e) => {
  if (e.target.closest('[data-skip]')) {
    skipReview();
    return;
  }
  const button = e.target.closest('[data-review]');
  if (!button) return;
  const article = button.closest('article');
  article.querySelectorAll('button').forEach((b) => (b.disabled = true));
  try {
    const event = state.events.find((item) => item.id === button.dataset.review);
    const saved = await api('/api/review', {
      id: button.dataset.review,
      status: button.dataset.status,
      actual: 0,
      expected_status: event?.status,
      note: $('review-note')?.value || '',
    });
    rememberReview(saved.review_id, event);
    await refresh();
    renderAlerts();
    renderCamera();
    toast('تسجلات المراجعة، داز التنبيه الموالي.');
  } catch (err) {
    if (err.status === 409) {
      await refresh();
      $('alerts-content').dataset.reviewKey = '';
      renderAlerts();
      renderCamera();
    }
    toast(err.message, true);
    article.querySelectorAll('button').forEach((b) => (b.disabled = false));
  }
};
async function pollJob() {
  try {
    state.job = await api('/api/jobs/' + state.jobId);
    await refresh();
    renderCamera();
    renderProgress();
    renderAlerts();
    if (state.job.status === 'running') {
      setBusy(!state.job.live);
      setTimeout(pollJob, 1500);
    } else {
      setBusy(false);
      loadRuns();
      toast(state.job.message, state.job.status === 'error');
    }
  } catch (err) {
    if (err.status === 404) {
      setBusy(false);
      sessionStorage.removeItem('shopguard-job');
      toast('التحليل السابق ما بقى متوفرش. اختار فيديو جديد.', true);
      return;
    }
    toast('الاتصال تقطع، كنعاودو نتصلو بالتحليل…', true);
    setTimeout(pollJob, 4000);
  }
}
async function init() {
  document.querySelectorAll('[data-icon]').forEach((el) => (el.innerHTML = icon(el.dataset.icon)));
  try {
    const first = await api('/api/state?mode=demo');
    state.mode = first.settings.demo ? 'demo' : 'real';
    state.settings = first.settings;
    if (state.mode === 'demo') state.events = first.events;
    else await refresh();
    state.jobId =
      sessionStorage.getItem('shopguard-job') || new URLSearchParams(location.search).get('job');
    if (state.jobId) {
      try {
        state.job = await api('/api/jobs/' + state.jobId);
        sessionStorage.setItem('shopguard-job', state.jobId);
        state.mode = 'real';
        await refresh();
      } catch {
        sessionStorage.removeItem('shopguard-job');
        state.jobId = null;
      }
    }
    await loadRuns();
    renderCamera();
    renderProgress();
    renderAlerts();
    if (state.job?.status === 'running') {
      setBusy(!state.job.live);
      pollJob();
    }
  } catch (err) {
    toast('ما قدرناش نتصلو بالتطبيق. تأكد أن app.py خدام.', true);
  }
}
init();
setInterval(() => {
  if (!state.busy && !state.file) loadRuns();
}, 15000);
