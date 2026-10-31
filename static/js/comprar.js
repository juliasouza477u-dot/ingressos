const app = document.getElementById('app');
const P = { front: +app.dataset.front, general: +app.dataset.general, popcorn: +app.dataset.popcorn, coke: +app.dataset.coke };
const picked = new Set();
const $ = n => app.querySelector(`[name=${n}]`);

function update() {
  $('seats').value = [...picked].join(',');
  const t = picked.size * P.front + $('general').value * P.general
          + $('popcorn').value * P.popcorn + $('coke').value * P.coke;
  document.getElementById('total').textContent = 'R$ ' + t;
}
app.querySelectorAll('.seat').forEach(b => b.onclick = () => {
  const s = b.dataset.seat;
  picked.has(s) ? picked.delete(s) : picked.add(s);
  b.classList.toggle('on');
  update();
});
app.querySelectorAll('.qty button').forEach(b => b.onclick = () => {
  const i = b.parentElement.querySelector('input');
  i.value = Math.max(0, Math.min(50, +i.value + +b.dataset.d));
  update();
});
$('seats') ; update();
