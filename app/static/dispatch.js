const note = document.querySelector('#dispatch-note');

function formPayload(form) {
  const payload = {};
  for (const field of new FormData(form).entries()) {
    const [key, value] = field;
    if (value.trim() !== '') payload[key] = value.trim();
  }
  return payload;
}

async function post(form, url, okText) {
  const button = form.querySelector('button');
  button.disabled = true;
  note.hidden = false;
  note.textContent = '正在提交调度操作';
  try {
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(formPayload(form))});
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || '操作失败');
    note.textContent = `${okText}${data.reused ? '（已存在，复用同一凭据/结果）' : ''}`;
    window.setTimeout(() => window.location.reload(), 650);
  } catch (problem) {
    note.textContent = problem.message;
    button.disabled = false;
  }
}

document.querySelector('#release-form')?.addEventListener('submit', event => {
  event.preventDefault();
  post(event.target, '/api/dispatch/releases', '放行时段已发布');
});
document.querySelector('#backfill-form')?.addEventListener('submit', event => {
  event.preventDefault();
  post(event.target, '/api/dispatch/backfills', '补报已入账');
});
