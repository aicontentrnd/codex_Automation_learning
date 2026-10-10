const form = document.querySelector('#form');
const status = document.querySelector('#status');
const result = document.querySelector('#result');
const retryButton = document.querySelector('#retry');
let pollTimer, imageUrl, currentJob;
let generation = 0;
let pendingRequest;

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const active = ++generation;
  clearTimeout(pollTimer);
  if (imageUrl) URL.revokeObjectURL(imageUrl);
  result.hidden = true;
  retryButton.hidden = true;
  status.textContent = 'Creating request…';
  const body = { prompt: document.querySelector('#prompt').value };
  const ratio = document.querySelector('#ratio').value.trim();
  if (ratio) body.aspect_ratio = ratio;
  const serialized = JSON.stringify(body);
  if (!pendingRequest || pendingRequest.body !== serialized) {
    pendingRequest = { key: crypto.randomUUID(), body: serialized };
  }
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const response = await fetch('/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': pendingRequest.key },
      body: serialized,
    });
    const job = await response.json();
    if (!response.ok) throw new Error(job.error || 'Request failed');
    if (active !== generation) return;
    pendingRequest = null;
    currentJob = job;
    await poll(job.job_id, active);
  } catch (error) {
    if (active === generation) status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

async function poll(id, active) {
  if (active !== generation) return;
  try {
    const response = await fetch(`/jobs/${id}`);
    const job = await response.json();
    if (active !== generation) return;
    if (!response.ok) throw new Error(job.error || 'Status request failed');
    currentJob = job;
    status.textContent = `Request ${id}: ${job.status}${job.error_message ? ` — ${job.error_message}` : ''}`;
    const expired = job.processing_expires_at && Date.parse(job.processing_expires_at) <= Date.now();
    const stalled = job.status === 'pending' && !job.deliveries.queued && !job.deliveries.sending;
    retryButton.hidden = !(job.status === 'failed' || stalled || expired);
    if (stalled && !Object.keys(job.deliveries).length) {
      status.textContent += '. Subscribe in ChatGPT Work, then retry this request.';
    }
    if (job.status === 'completed') {
      const imageResponse = await fetch(`/jobs/${id}/image`);
      if (!imageResponse.ok) throw new Error('Image download failed');
      const blob = await imageResponse.blob();
      if (active !== generation) return;
      imageUrl = URL.createObjectURL(blob);
      result.src = imageUrl;
      result.hidden = false;
    } else if (job.status !== 'failed') {
      pollTimer = setTimeout(() => poll(id, active), 3000);
    }
  } catch (error) {
    if (active === generation) status.textContent = error.message;
  }
}

retryButton.addEventListener('click', async () => {
  if (!currentJob) return;
  retryButton.disabled = true;
  try {
    const response = await fetch(`/jobs/${currentJob.job_id}/retry`, {
      method: 'POST', headers: { 'Idempotency-Key': `retry-${currentJob.job_id}-${currentJob.attempt}` },
    });
    const job = await response.json();
    if (!response.ok) throw new Error(job.error || 'Retry failed');
    clearTimeout(pollTimer);
    await poll(job.job_id, ++generation);
  } catch (error) { status.textContent = error.message; }
  finally { retryButton.disabled = false; }
});
