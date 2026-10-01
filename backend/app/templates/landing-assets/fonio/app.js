'use strict';
const menuButton = document.querySelector('.mobile-toggle');
const navigation = document.querySelector('#navigation');
function closeNavigation() { navigation.classList.remove('is-open'); menuButton.setAttribute('aria-expanded','false'); menuButton.setAttribute('aria-label','Open navigation'); }
menuButton.addEventListener('click', () => { const open = navigation.classList.toggle('is-open'); menuButton.setAttribute('aria-expanded', String(open)); menuButton.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation'); });
const menus = [...document.querySelectorAll('.nav-menu')];
menus.forEach(menu => menu.addEventListener('toggle', () => { if (menu.open) menus.forEach(other => { if (other !== menu) other.open = false; }); }));
document.addEventListener('click', event => { menus.forEach(menu => { if (!menu.contains(event.target)) menu.open = false; }); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') { const openMenu = menus.find(menu => menu.open); if (openMenu) { openMenu.open = false; openMenu.querySelector('summary').focus(); } else if(navigation.classList.contains('is-open')) { closeNavigation(); menuButton.focus(); } } });
navigation.querySelectorAll('a').forEach(a => a.addEventListener('click',closeNavigation));
matchMedia('(min-width:1251px)').addEventListener('change', event => { if(event.matches) closeNavigation(); });
const country = document.querySelector('#country');
const phone = document.querySelector('#phone');
const form = document.querySelector('#phone-demo');
const error = document.querySelector('#phone-error');
const dialog = document.querySelector('#simulation');
const flagPaths = {US:'us.svg',GB:'en.svg',AT:'at.svg',BR:'pt-br.svg',FR:'fr.svg',DE:'de.png',IT:'it.svg',NL:'nl.svg',PL:'pl.svg',ES:'es.svg',CH:'ch.svg'};
function clearError() { form.classList.remove('has-error'); phone.removeAttribute('aria-invalid'); error.textContent = ''; }
country.addEventListener('change', () => { document.querySelector('.country-flag').src = 'assets/'+flagPaths[country.value]; clearError(); });
phone.addEventListener('input',clearError);
function showError() { form.classList.add('has-error'); phone.setAttribute('aria-invalid','true'); error.textContent = 'Please enter a valid phone number.'; }
form.addEventListener('submit', event => { event.preventDefault(); const value = phone.value.trim(); let valid = false; try { const number = libphonenumber.parsePhoneNumberFromString(value,country.value); valid = /^[+\d\s().-]+$/.test(value) && Boolean(number?.isValid()); } catch { valid = false; } if (!valid) { showError(); phone.focus(); return; } clearError(); dialog.showModal(); });
document.querySelector('#finish-demo').addEventListener('click', () => dialog.close());
// Reproducible visual fixture; default visits always start with a clean form.
if (new URLSearchParams(location.search).get('state') === 'error') showError();
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const decorativeVideos = [...document.querySelectorAll('.hero video')];
const motionButton = document.querySelector('.motion-toggle');
let paused = reducedMotion.matches;
function updateMotion() { decorativeVideos.forEach(video => { if(paused || document.hidden) video.pause(); else video.play().catch(() => {}); }); motionButton.setAttribute('aria-pressed',String(paused)); motionButton.setAttribute('aria-label',paused ? 'Play decorative animation' : 'Pause decorative animation'); motionButton.querySelector('img').src = 'assets/'+(paused ? 'play-fill.svg' : 'pause-fill.svg'); }
motionButton.addEventListener('click', () => { paused = !paused; updateMotion(); });
reducedMotion.addEventListener('change', event => { paused = event.matches; updateMotion(); });
document.addEventListener('visibilitychange',updateMotion);
updateMotion();
const track = document.querySelector('.reviews-track');
function moveReviews(direction) { const width = track.querySelector('article').getBoundingClientRect().width + 22; track.scrollBy({left:direction*width,behavior:reducedMotion.matches ? 'instant':'smooth'}); }
document.querySelector('#previous-review').addEventListener('click',() => moveReviews(-1));
document.querySelector('#next-review').addEventListener('click',() => moveReviews(1));
const platformVideo = document.querySelector('#platform-video');
platformVideo.addEventListener('error',() => { document.querySelector('.video-status').textContent = 'The platform tour could not load. Please try again later or book a demo.'; });
