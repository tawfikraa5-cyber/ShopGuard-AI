const liveCapture = {
  id: null,
  stream: null,
  starting: false,
  generation: 0,
  timer: null,
  request: null,
  failures: 0,
  dwell: 15,
  devices: [],
};
const cameraMessages = {
  NotAllowedError: 'خاص تسمح للمتصفح يستعمل الكاميرا. تقدر تبدّل الصلاحية من إعدادات الموقع.',
  NotFoundError: 'ما لقيناش كاميرا متصلة بهاد الجهاز.',
  NotReadableError: 'الكاميرا مستعملة من تطبيق آخر أو ما قدرناش نفتحوها.',
  OverconstrainedError: 'هاد الكاميرا ما كتدعمش الإعدادات المطلوبة.',
  SecurityError: 'المتصفح ما سمحش بالوصول للكاميرا. جرّب Chrome أو Edge على localhost.',
};
function cameraButton() {
  const button = document.getElementById('start-camera');
  button.textContent = liveCapture.id
    ? 'وقف الكاميرا'
    : liveCapture.starting
      ? 'إلغاء طلب الكاميرا'
      : 'كاميرا مباشرة';
  button.classList.toggle('camera-active', !!liveCapture.id);
}
function renderLiveCamera() {
  const stage = $('camera-stage');
  if (!$('live-video')) {
    stage.innerHTML =
      '<video id="live-video" autoplay muted playsinline aria-label="البث المباشر من الكاميرا"></video><canvas id="live-overlay" aria-hidden="true"></canvas><div class="zone-layer" id="zone-layer" aria-label="جرّ باش تحدّد المنطقة الحساسة"><div class="zone-overlay" id="zone-overlay"><span>المنطقة الحساسة</span></div></div>';
    const video = $('live-video');
    video.srcObject = liveCapture.stream;
    video.onloadedmetadata = () => {
      stage.style.aspectRatio = `${video.videoWidth}/${video.videoHeight}`;
    };
    video.play().catch(() => {});
    bindZone();
  }
  $('zone-overlay').style.cssText = zoneStyle(state.zone);
  $('live-controls').hidden = false;
  $('video-setup').hidden = true;
  $('review-tools').hidden = true;
  $('camera-status').textContent = 'مباشر · الكاميرا شاعلة';
  $('source-name').textContent = 'بث مباشر · ' + videoTime(state.job?.second || 0);
  $('people-count').innerHTML = icon('users') + ' ' + (state.job?.people || 0) + ' أشخاص';
  $('mode-note').textContent = 'الكاميرا شاعلة، والتحليل محلي. التنبيهات خاصّها مراجعتك.';
  cameraButton();
}
function drawLiveBoxes(boxes) {
  const video = $('live-video'),
    canvas = $('live-overlay');
  if (!video || !canvas) return;
  canvas.width = video.videoWidth || 640;
  canvas.height = video.videoHeight || 480;
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.strokeStyle = '#4fdfb2';
  context.fillStyle = '#4fdfb2';
  context.lineWidth = 2;
  context.font = '14px sans-serif';
  for (const item of boxes) {
    const [a, b, c, d] = item.box;
    context.strokeRect(
      a * canvas.width,
      b * canvas.height,
      (c - a) * canvas.width,
      (d - b) * canvas.height,
    );
    context.fillText(
      '#' + item.id + ' ' + Math.round(item.confidence * 100) + '%',
      a * canvas.width,
      Math.max(16, b * canvas.height - 5),
    );
  }
}
async function startLiveCamera(deviceId) {
  if (liveCapture.starting) {
    liveCapture.generation++;
    liveCapture.stream?.getTracks().forEach((track) => track.stop());
    liveCapture.stream = null;
    liveCapture.starting = false;
    setBusy(false);
    cameraButton();
    return;
  }
  if (liveCapture.id) {
    await stopLiveCamera();
    return;
  }
  if (state.busy) {
    toast('وقف التحليل اللي خدام الأول باش تشغّل الكاميرا.');
    return;
  }
  const generation = ++liveCapture.generation;
  liveCapture.starting = true;
  setBusy(true);
  cameraButton();
  let stream;
  try {
    if (!navigator.mediaDevices?.getUserMedia)
      throw Error(
        'الكاميرا ما مدعومةش هنا. حل التطبيق فـ Chrome أو Edge على http://127.0.0.1:8001.',
      );
    stream = await navigator.mediaDevices.getUserMedia({
      video: deviceId
        ? { deviceId: { exact: deviceId }, width: { ideal: 1280 }, height: { ideal: 720 } }
        : { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: { ideal: 'environment' } },
      audio: false,
    });
    if (generation !== liveCapture.generation) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    liveCapture.stream = stream;
    state.zone = [...state.settings.zone];
    liveCapture.dwell = state.settings.dwell_seconds;
    $('live-dwell').value = liveCapture.dwell;
    const result = await api('/api/live/start', {
      analysis_options: { zone: state.zone, dwell_seconds: liveCapture.dwell },
    });
    if (generation !== liveCapture.generation) {
      stream.getTracks().forEach((track) => track.stop());
      await api('/api/live/stop', { id: result.id });
      return;
    }
    liveCapture.id = result.id;
    liveCapture.starting = false;
    liveCapture.failures = 0;
    state.file = null;
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = null;
    state.jobId = result.id;
    state.mode = 'real';
    state.job = {
      status: 'running',
      live: true,
      people: 0,
      message: 'كنوجدو تحليل الكاميرا…',
      analysis_config: { zone: state.zone, dwell_seconds: liveCapture.dwell },
    };
    sessionStorage.setItem('shopguard-job', result.id);
    await refresh();
    renderCamera();
    renderProgress();
    renderAlerts();
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter(
      (item) => item.kind === 'videoinput',
    );
    liveCapture.devices = devices;
    $('live-device').innerHTML = devices
      .map(
        (item, i) =>
          `<option value="${esc(item.deviceId)}">${esc(item.label || 'كاميرا ' + (i + 1))}</option>`,
      )
      .join('');
    $('live-device').value = stream.getVideoTracks()[0].getSettings().deviceId || '';
    stream.getVideoTracks()[0].addEventListener('ended', () => {
      if (liveCapture.id) stopLiveCamera('الكاميرا تقطعات من الجهاز.');
    });
    sendLiveFrame(generation);
  } catch (error) {
    if (stream) stream.getTracks().forEach((track) => track.stop());
    if (generation === liveCapture.generation) {
      if (liveCapture.id) {
        const id = liveCapture.id;
        await api('/api/live/stop', { id }).catch(() => {});
        state.job = await api('/api/jobs/' + id).catch(() => ({ ...state.job, status: 'error' }));
      }
      liveCapture.id = null;
      liveCapture.stream = null;
      liveCapture.starting = false;
      setBusy(false);
      cameraButton();
      $('live-controls').hidden = true;
      renderCamera();
      renderProgress();
      renderAlerts();
      toast(cameraMessages[error.name] || error.message || 'ما قدرناش نشغلو الكاميرا.', true);
    }
  }
}
async function sendLiveFrame(generation) {
  if (generation !== liveCapture.generation || !liveCapture.id) return;
  try {
    const video = $('live-video');
    if (!video || !video.videoWidth) {
      liveCapture.timer = setTimeout(() => sendLiveFrame(generation), 300);
      return;
    }
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, 640 / video.videoWidth);
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.8));
    if (!blob) throw Error('ما قدرناش نقراو صورة الكاميرا.');
    const controller = new AbortController();
    liveCapture.request = controller;
    const response = await fetch('/api/live/frame?id=' + liveCapture.id, {
      method: 'POST',
      headers: {
        'Content-Type': 'image/jpeg',
        'X-Zone': JSON.stringify(state.zone),
        'X-Dwell-Seconds': String(liveCapture.dwell),
      },
      body: blob,
      signal: controller.signal,
    });
    const result = await response.json();
    if (generation !== liveCapture.generation) return;
    if (!response.ok) throw Error(result.error || 'الاتصال بالكاميرا تقطع.');
    liveCapture.failures = 0;
    state.job.message = result.message || 'الكاميرا خدامة. كنحللو الحركة مباشرة.';
    if (result.ready) {
      state.job.people = result.people;
      state.job.second = result.second;
      state.job.analysis_config = { zone: state.zone, dwell_seconds: liveCapture.dwell };
      drawLiveBoxes(result.boxes);
      await refresh();
      renderAlerts();
      renderLiveCamera();
    }
    renderProgress();
  } catch (error) {
    if (generation !== liveCapture.generation) return;
    if (++liveCapture.failures >= 4) {
      await stopLiveCamera(error.message);
      return;
    }
    state.job.message = 'الاتصال تقطع لحظة، كنعاودو نتصلو…';
    renderProgress();
  }
  if (generation === liveCapture.generation)
    liveCapture.timer = setTimeout(() => sendLiveFrame(generation), 350);
}
async function stopLiveCamera(message) {
  const id = liveCapture.id;
  liveCapture.generation++;
  clearTimeout(liveCapture.timer);
  liveCapture.request?.abort();
  liveCapture.stream?.getTracks().forEach((track) => track.stop());
  liveCapture.stream = null;
  liveCapture.id = null;
  liveCapture.starting = false;
  cameraButton();
  $('live-controls').hidden = true;
  try {
    if (id) {
      await api('/api/live/stop', { id });
      state.job = await api('/api/jobs/' + id);
      await api('/api/settings', {
        zone: state.zone,
        dwell_seconds: liveCapture.dwell,
        demo: false,
      }).catch(() => {});
      await refresh();
    }
  } catch {}
  setBusy(false);
  renderCamera();
  renderProgress();
  $('alerts-content').dataset.reviewKey = '';
  renderAlerts();
  loadRuns();
  if (message) toast(message, true);
  else toast('الكاميرا توقفات. التنبيهات باقين محفوظين.');
}
document.getElementById('start-camera').onclick = () => startLiveCamera();
document.getElementById('live-dwell').onchange = (event) => {
  const value = Number(event.target.value);
  if (Number.isFinite(value) && value >= 1 && value <= 300) liveCapture.dwell = value;
  else {
    event.target.value = liveCapture.dwell;
    toast('المدة خاصها تكون بين 1 و300 ثانية.', true);
  }
};
document.getElementById('live-device').onchange = async (event) => {
  const deviceId = event.target.value;
  await stopLiveCamera();
  await startLiveCamera(deviceId);
};
window.addEventListener('pagehide', () => {
  liveCapture.stream?.getTracks().forEach((track) => track.stop());
  if (liveCapture.id)
    navigator.sendBeacon(
      '/api/live/stop',
      new Blob([JSON.stringify({ id: liveCapture.id })], { type: 'application/json' }),
    );
});
