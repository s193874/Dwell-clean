const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

assert(
  html.includes('if (dayRec.menstrual)') && !html.includes("dayRec.flow === 'menstrual'"),
  'calendar must render the independent menstrual field',
);
assert(
  html.includes("mensBtn.onclick = () => saveDay({ menstrual: !curMenstrual })"),
  'calendar toggle must persist menstrual without overwriting flow',
);
assert(
  html.includes("menstrualDays.has(date)") && html.includes("marker.className = 'tl-menstrual'"),
  'diary timeline must mark menstrual dates',
);
assert(
  html.includes('.cal-day.menstrual:not(.today) .dnum'),
  'menstrual dates must use a pink circular day background',
);

console.log('calendar menstrual field and diary timeline runtime contract passed');
