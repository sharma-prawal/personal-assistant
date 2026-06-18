GOOGLE_CHAT_UNREAD_JS = """() => {
    const _NOISE_RE = /press tab for more options|open in a pop-up|\\d+\\s+notification|^\\d+$/i;
    const items = Array.from(document.querySelectorAll('[role="listitem"]'));
    const spaceNames = new Set();
    for (const item of items) {
        const text = item.innerText || '';
        if (!text.toLowerCase().includes('unread')) continue;
        const parts = text.split('\\n').map(s => s.trim()).filter(Boolean);
        if (parts[0] === 'Unread' && parts.includes('Space')) {
            spaceNames.add(parts[1] || '');
        }
    }
    const map = new Map();
    for (const item of items) {
        const text = item.innerText || '';
        if (!text.toLowerCase().includes('unread')) continue;
        const parts = text.split('\\n').map(s => s.trim()).filter(Boolean);
        let name = '';
        let preview = '';
        let score = 0;
        if (parts[0] === 'Unread' && parts.length > 1) {
            name = parts[1] || '';
            score = 1;
        } else {
            name = parts[0] || '';
            const ui = parts.findIndex(p => p === 'Unread');
            if (ui >= 0 && ui + 1 < parts.length) {
                preview = parts.slice(ui + 1)
                    .filter(p => !_NOISE_RE.test(p))
                    .join(' ');
            }
            score = 2;
        }
        if (!name || name === 'Unread') continue;
        const isSpace = spaceNames.has(name) || text.includes(' Space');
        const existing = map.get(name);
        if (!existing || score > existing.score) {
            map.set(name, { from: name.substring(0, 60), preview: preview.substring(0, 200), type: isSpace ? 'space' : 'dm', score });
        }
    }
    return Array.from(map.values()).map(({from, preview, type}) => ({from, preview, type}));
}"""


OUTLOOK_MAIL_UNREAD_JS = """() => {
    const results = [];
    let items = Array.from(document.querySelectorAll('[role="option"]'));
    if (!items.length) items = Array.from(document.querySelectorAll('[data-convid], [data-automation-id="MailListItem"]'));
    if (!items.length) items = Array.from(document.querySelectorAll('[class*="MailListItem"]'));
    console.log('[outlook-mail-js] candidate items:', items.length);
    for (const el of items.slice(0, 30)) {
        try {
            const rawAria = el.getAttribute('aria-label') || '';
            const aria = rawAria.toLowerCase();
            const txt  = (el.innerText || '').substring(0, 500);
            const hasUnread = aria.includes('unread') ||
                !!el.querySelector('[class*="nread"], [data-icon-name="MailUnread"], [class*="badge"], [class*="Badge"]');
            if (!hasUnread) continue;
            const cleaned = rawAria.replace(/^unread[,\\s]*/i, '');
            const parts   = cleaned.split(/[;\\n]/);
            let sender  = (parts[0] || '').trim();
            let subject = (parts[1] || '').trim();
            let preview = (parts[2] || '').trim();
            if (!sender || !subject) {
                const lines = txt.split('\\n').map(s => s.trim()).filter(Boolean);
                sender  = sender  || lines[0] || '';
                subject = subject || lines[1] || '';
                preview = preview || lines[2] || '';
            }
            if (sender || subject) {
                results.push({ sender: sender.substring(0,80), subject: subject.substring(0,120), snippet: preview.substring(0,250) });
            }
        } catch(e) {}
    }
    return results;
}"""


TEAMS_CHAT_FALLBACK_JS = """() => {
    const results = [];
    const seen = new Set();

    const unreadByAria = Array.from(document.querySelectorAll('[aria-label]'))
        .filter(el => (el.getAttribute('aria-label') || '').toLowerCase().includes('unread'));

    const allEls = Array.from(document.querySelectorAll('*'));
    const badgeEls = allEls.filter(el => {
        const txt = (el.innerText || '').trim();
        if (!/^\\d{1,3}$/.test(txt)) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.width < 60 && rect.height > 0 && rect.height < 60;
    });

    const rowCandidates = new Set();
    for (const badge of badgeEls) {
        let node = badge.parentElement;
        for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
            const rect = node.getBoundingClientRect();
            if (rect.width > 150 && rect.height > 30 && rect.height < 250) {
                rowCandidates.add(node);
                break;
            }
        }
    }
    for (const el of unreadByAria) rowCandidates.add(el);

    for (const el of rowCandidates) {
        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
        const lines = (el.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
        const name = lines[0] || aria.split(',')[0].replace(/unread/i, '').trim();
        if (!name || name.length < 2 || name.length > 80) continue;
        if (/^\\d+$/.test(name)) continue;
        if (seen.has(name)) continue;
        seen.add(name);
        const preview = lines.slice(1, 4).join(' ').substring(0, 200);
        results.push({ chat: name, preview });
        if (results.length >= 8) break;
    }
    return results;
}"""


OUTLOOK_CALENDAR_FALLBACK_JS = """() => {
    const results = [];
    const seen = new Set();
    const mainArea = document.querySelector(
        '[role="main"], [class*="calendarGrid"], [class*="dayView"], [class*="DayView"], ' +
        '[class*="timeGrid"], [class*="TimeGrid"], main'
    ) || document.body;
    const candidates = Array.from(mainArea.querySelectorAll(
        '[data-eventid], [class*="eventItem"], [class*="calendarEvent"], ' +
        '[class*="EventCard"], [class*="event-wrapper"], [class*="eventWrapper"]'
    ));
    console.log('[outlook-cal-js] main-area candidates:', candidates.length, 'in', mainArea.tagName);
    if (!candidates.length) {
        const btns = Array.from(mainArea.querySelectorAll('[role="button"][aria-label]'));
        for (const btn of btns) {
            const aria = btn.getAttribute('aria-label') || '';
            if (aria.length > 50 &&
                /(\\d{1,2}(?::\\d{2})?\\s*[AP]M)/i.test(aria) &&
                !aria.toLowerCase().includes('new event') &&
                !aria.toLowerCase().includes('working location')) {
                candidates.push(btn);
            }
        }
        console.log('[outlook-cal-js] button candidates after filter:', candidates.length);
    }
    for (const el of candidates) {
        const aria  = el.getAttribute('aria-label') || '';
        const inner = (el.innerText || '').replace(/\\n+/g, ' ').trim();
        const label = (aria.length > 20 ? aria : inner).trim();
        if (!label || label.length < 10 || seen.has(label)) continue;
        const low = label.toLowerCase();
        if (['previous','next','today','month','week','day','search','settings',
             'new event','home','office','wfh','show more','working location'].some(
                 w => low === w || low.startsWith(w + ' '))) continue;
        const timeMatch = label.match(/(\\d{1,2}(?::\\d{2})?\\s*[AP]M)/i);
        seen.add(label);
        results.push({
            title: label.substring(0, 120),
            time:  timeMatch ? timeMatch[1].toUpperCase() : '',
        });
        if (results.length >= 10) break;
    }
    return results;
}"""


OUTLOOK_CALENDAR_BODY_TEXT_JS = """() => {
    const lines = document.body.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
    let hourEnd = -1;
    for (let i = 0; i < lines.length - 1; i++) {
        if (lines[i] === '22' && lines[i + 1] === '23') { hourEnd = i + 2; break; }
    }
    if (hourEnd === -1) {
        for (let i = 0; i < lines.length - 1; i++) {
            if (/^11\\s*PM$/i.test(lines[i])) { hourEnd = i + 1; break; }
        }
    }
    console.log('[cal-bt] hourEnd=' + hourEnd + ' totalLines=' + lines.length);
    if (hourEnd === -1) return [];

    const NOISE = new Set(['microsoft teams meeting', 'microsoft teams', 'teams meeting']);
    const NAME_RE = /^[A-Z][a-zA-Z'\\-]+ [A-Z][a-zA-Z'\\-]+$/;
    const SKIP_PREFIX = ['loaded ', 'ooo ', 'out of office'];
    const seen = new Set();
    const titles = [];
    for (let i = hourEnd; i < lines.length; i++) {
        const line = lines[i];
        const low = line.toLowerCase();
        if (!line || line.length < 5) continue;
        if (/^\\d+$/.test(line)) continue;
        if (NOISE.has(low)) continue;
        if (SKIP_PREFIX.some(p => low.startsWith(p))) continue;
        if (NAME_RE.test(line)) continue;
        if (line.includes(';') || line.includes('@')) continue;
        if (/\\b(Room|Kitchen|Floor|Office|Building)\\b/i.test(line)) continue;
        if (seen.has(line)) continue;
        seen.add(line);
        titles.push(line);
    }
    console.log('[cal-bt] raw titles: ' + titles.join(' | '));
    return titles;
}"""
