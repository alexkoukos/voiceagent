const form = document.getElementById('lead-form');
const status = document.getElementById('form-status');
const contact = document.getElementById('contact-detail');
const contactLabel = document.getElementById('contact-label');
const demoStart = document.getElementById('demo-start');

function updateContact() {
  const method = form.elements.contact_method.value;
  contact.type = method === 'email' ? 'email' : 'tel';
  contact.autocomplete = method === 'email' ? 'email' : 'tel';
  contactLabel.firstChild.textContent = method === 'email' ? 'Email επικοινωνίας ' : 'Τηλέφωνο επικοινωνίας ';
  contact.value = '';
}

form.querySelectorAll('[name="contact_method"]').forEach(input => input.addEventListener('change', updateContact));

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  status.className = 'form-status';
  status.textContent = 'Στέλνουμε το αίτημά σας…';
  try {
    const response = await fetch(form.action, {
      method: 'POST',
      headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
      body: JSON.stringify(Object.fromEntries(new FormData(form))),
    });
    if (!response.ok) {
      let message = 'Δεν μπορέσαμε να στείλουμε το αίτημα. Δοκιμάστε ξανά.';
      try { const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; } catch {}
      throw new Error(message);
    }
    status.textContent = 'Λάβαμε το αίτημά σας. Θα επικοινωνήσουμε μαζί σας για να οργανώσουμε το demo.';
    form.reset();
    updateContact();
    form.elements.request_id.value = crypto.randomUUID();
  } catch (error) {
    status.textContent = error.message;
    status.className = 'form-status error';
  } finally {
    submit.disabled = false;
  }
});

if (status.dataset.sent === 'true') {
  status.textContent = 'Λάβαμε το αίτημά σας. Θα επικοινωνήσουμε μαζί σας για να οργανώσουμε το demo.';
}

if (demoStart) {
  demoStart.addEventListener('click', () => {
    const frame = document.querySelector('#demo-panel iframe');
    if (!frame) return;
    frame.src = frame.dataset.src;
    document.getElementById('demo-panel').classList.add('active');
  });
}
