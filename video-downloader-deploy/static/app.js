(() => {
  'use strict';

  const byId = (id) => document.getElementById(id);
  const form = byId('analyze-form');
  const urlInput = byId('video-url');
  const analyzeButton = byId('analyze-button');
  const analyzeLabel = byId('analyze-label');
  const activity = byId('activity');
  const fieldError = byId('field-error');
  const result = byId('result');
  const quality = byId('quality');
  const downloadButton = byId('download-button');
  const downloadError = byId('download-error');
  const authorized = byId('authorized');
  const transfer = byId('transfer');
  const progress = byId('progress');
  const percentage = byId('percentage');
  const transferMessage = byId('transfer-message');
  const transferDetail = byId('transfer-detail');
  const saveLink = byId('save-link');
  const resumeButton = byId('resume-button');
  let analysis = null;
  let busy = false;
  let activeJob = null;
  let polling = false;
  let analysisUrl = '';

  async function request(path, payload, timeout = 15000) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {
        method: payload === undefined ? 'GET' : 'POST',
        headers: payload === undefined ? {} : { 'Content-Type': 'application/json' },
        body: payload === undefined ? undefined : JSON.stringify(payload),
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const message = typeof data.detail === 'string' ? data.detail :
          typeof data.error === 'string' ? data.error : 'The request could not be completed. Try again.';
        throw new Error(message);
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The request timed out. Check your connection and try again.');
      if (error instanceof TypeError) throw new Error('Cannot reach the local app. Keep its server window open and try again.');
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function lock(locked, phase = '') {
    busy = locked;
    urlInput.disabled = locked;
    analyzeButton.disabled = locked;
    quality.disabled = locked;
    authorized.disabled = locked;
    downloadButton.disabled = locked;
    analyzeLabel.textContent = phase === 'analyze' ? 'Analyzing…' : 'Analyze link';
    byId('download-label').textContent = phase === 'download' ? 'Preparing…' : 'Download video';
  }

  function durationLabel(seconds) {
    if (!Number.isFinite(seconds)) return null;
    const time = Math.floor(seconds);
    const hours = Math.floor(time / 3600);
    const minutes = Math.floor((time % 3600) / 60);
    const secs = String(time % 60).padStart(2, '0');
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${secs}` : `${minutes}:${secs}`;
  }

  function sizeLabel(bytes, estimated) {
    if (!Number.isFinite(bytes) || bytes <= 0) return 'Size unknown';
    const amount = bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${estimated ? '~' : ''}${amount}`;
  }

  function updateQualityHint() {
    const selected = analysis?.options.find((option) => option.id === quality.value);
    byId('quality-hint').textContent = selected ?
      `Video and audio included · ${sizeLabel(selected.size_bytes, selected.estimated)}` : 'Video and audio included.';
  }

  function displayVideo(data) {
    analysis = data;
    analysisUrl = urlInput.value.trim();
    byId('video-title').textContent = data.title;
    byId('video-meta').textContent = [data.channel, durationLabel(data.duration)].filter(Boolean).join(' · ');
    const image = byId('thumbnail');
    image.hidden = true;
    byId('thumbnail-fallback').hidden = false;
    image.onload = () => { image.hidden = false; byId('thumbnail-fallback').hidden = true; };
    image.onerror = () => { image.hidden = true; byId('thumbnail-fallback').hidden = false; };
    if (typeof data.thumbnail === 'string' && /^https:\/\/i\.ytimg\.com\//.test(data.thumbnail)) image.src = data.thumbnail;
    else image.removeAttribute('src');
    quality.replaceChildren();
    for (const option of data.options) {
      const element = document.createElement('option');
      element.value = option.id;
      element.textContent = option.label.toUpperCase().includes(option.ext.toUpperCase()) ? option.label : `${option.label} · ${option.ext.toUpperCase()}`;
      quality.appendChild(element);
    }
    updateQualityHint();
    result.hidden = false;
    byId('video-title').focus({ preventScroll: true });
  }

  function clearErrors() {
    fieldError.textContent = '';
    downloadError.textContent = '';
    urlInput.removeAttribute('aria-invalid');
    authorized.removeAttribute('aria-invalid');
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    clearErrors();
    const value = urlInput.value.trim();
    if (!value) {
      fieldError.textContent = 'Paste a YouTube video link first.';
      urlInput.setAttribute('aria-invalid', 'true');
      urlInput.focus();
      return;
    }
    result.hidden = true;
    transfer.hidden = true;
    analysis = null;
    activeJob = null;
    saveLink.hidden = true;
    authorized.checked = false;
    lock(true, 'analyze');
    activity.textContent = 'Checking the video and available qualities…';
    const slow = window.setTimeout(() => { activity.textContent = 'YouTube is taking longer to respond. Still checking…'; }, 15000);
    try {
      const data = await request('/api/analyze', { url: value }, 110000);
      if (!Array.isArray(data.options) || !data.options.length) throw new Error('No downloadable video formats were found. Try another authorized public video.');
      displayVideo(data);
      activity.textContent = '';
    } catch (error) {
      activity.textContent = '';
      fieldError.textContent = error.message;
      urlInput.setAttribute('aria-invalid', 'true');
    } finally {
      window.clearTimeout(slow);
      lock(false);
    }
  });

  urlInput.addEventListener('input', () => {
    fieldError.textContent = '';
    urlInput.removeAttribute('aria-invalid');
    if (analysis && urlInput.value.trim() !== analysisUrl) {
      result.hidden = true;
      analysis = null;
    }
  });
  quality.addEventListener('change', updateQualityHint);
  authorized.addEventListener('change', () => {
    authorized.removeAttribute('aria-invalid');
    downloadError.textContent = '';
  });

  function renderJob(job) {
    transferMessage.textContent = job.message || 'Preparing your video…';
    const determined = typeof job.progress === 'number' && Number.isFinite(job.progress);
    if (determined) {
      progress.value = Math.max(0, Math.min(100, job.progress));
      percentage.textContent = `${Math.floor(progress.value)}%`;
    } else {
      progress.removeAttribute('value');
      percentage.textContent = '';
    }
    transferDetail.textContent = job.status === 'processing' ? 'Combining video and audio. Keep this page open.' :
      job.status === 'downloading' ? 'Preparing the file on your computer. Keep this page open.' : '';
  }

  async function pollJob() {
    if (!activeJob || polling) return;
    polling = true;
    resumeButton.hidden = true;
    const deadline = Date.now() + 42 * 60 * 1000;
    try {
      while (Date.now() < deadline) {
        const job = await request(`/api/jobs/${encodeURIComponent(activeJob)}`);
        renderJob(job);
        if (job.status === 'failed') {
          downloadError.textContent = job.message || 'The download failed. Analyze the link again to retry.';
          transferMessage.textContent = 'Download failed';
          transferDetail.textContent = 'Analyze the link again or choose another available quality.';
          progress.hidden = true;
          percentage.textContent = '';
          activeJob = null;
          lock(false);
          return;
        }
        if (job.status === 'ready') {
          const expected = `/api/files/${activeJob}`;
          if (job.download_url !== expected) throw new Error('The file link is invalid. Analyze the video again.');
          progress.value = 100;
          percentage.textContent = '';
          transferMessage.textContent = 'Your file is ready.';
          transferDetail.textContent = 'Download sent to your browser. If it did not start, use the link below.';
          saveLink.href = expected;
          saveLink.download = job.filename || 'video';
          saveLink.textContent = 'Save video';
          saveLink.hidden = false;
          saveLink.click();
          lock(false);
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
      }
      throw new Error('Status checking paused. Check again to see whether the file is ready.');
    } catch (error) {
      downloadError.textContent = error.message;
      transferMessage.textContent = 'Download status unavailable';
      transferDetail.textContent = 'The task may still be running. Check its status before starting another.';
      progress.hidden = true;
      percentage.textContent = '';
      resumeButton.hidden = false;
      // Keep the form locked: a lost connection is not proof that a job stopped.
    } finally {
      polling = false;
    }
  }

  downloadButton.addEventListener('click', async () => {
    if (busy || !analysis) return;
    downloadError.textContent = '';
    if (!authorized.checked) {
      downloadError.textContent = 'Confirm that you own the video or have permission to download it.';
      authorized.setAttribute('aria-invalid', 'true');
      authorized.focus();
      return;
    }
    lock(true, 'download');
    transfer.hidden = false;
    progress.hidden = false;
    progress.removeAttribute('value');
    percentage.textContent = '';
    saveLink.hidden = true;
    resumeButton.hidden = true;
    transferMessage.textContent = 'Starting your download…';
    transferDetail.textContent = '';
    try {
      const data = await request('/api/download', {
        analysis_id: analysis.analysis_id,
        option_id: quality.value,
        authorized: true,
      });
      activeJob = data.job_id;
      await pollJob();
    } catch (error) {
      transfer.hidden = true;
      downloadError.textContent = error.message;
      lock(false);
    }
  });

  resumeButton.addEventListener('click', () => {
    downloadError.textContent = '';
    progress.hidden = false;
    pollJob();
  });

  if (location.protocol === 'file:') {
    analyzeButton.disabled = true;
    activity.textContent = 'Run start.bat, then open http://127.0.0.1:8000. This app needs its local server.';
  }
})();
