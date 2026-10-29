"""
Builds the Chrome extension in extension/ (run again after changing autofill.py).
Copies the bot's own field-reading code into the extension and creates its private key.
"""
import json
import secrets
from pathlib import Path

import autofill

BOT = Path(__file__).resolve().parent
EXT = BOT / "extension"
EXT.mkdir(exist_ok=True)

token_file = EXT / "token.txt"
if not token_file.is_file():
    token_file.write_text(secrets.token_hex(24), encoding="utf-8")
token = token_file.read_text(encoding="utf-8").strip()

manifest = {
    "manifest_version": 3,
    "name": "Job Bot",
    "version": "1.0",
    "description": "Fills job application forms from your Job Bot profile. You review and submit.",
    "host_permissions": ["http://127.0.0.1:8765/*"],
    "background": {"service_worker": "background.js"},
    "action": {"default_title": "Fill this form with Job Bot"},
    "content_scripts": [{"matches": ["https://*/*"], "js": ["content.js"], "all_frames": True,
                         "run_at": "document_idle"}],
}
(EXT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

background = """// Talks to the Job Bot helper on this laptop (server.py). Only the extension can reach it.
const HELPER = 'http://127.0.0.1:8765';
const TOKEN = '%s';
// Toolbar icon: ask the page (and its frames) to fill.
chrome.action.onClicked.addListener(tab => chrome.tabs.sendMessage(tab.id, {cmd: 'fill'}).catch(() => {}));
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      const r = await fetch(HELPER + msg.path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-JobBot-Token': TOKEN},
        body: JSON.stringify(msg.payload || {}),
      });
      sendResponse({ok: r.ok, data: await r.json()});
    } catch (e) {
      sendResponse({ok: false, error: 'Job Bot helper is not running. Start it: start.bat > option 9.'});
    }
  })();
  return true;
});
""" % token
(EXT / "background.js").write_text(background, encoding="utf-8")

template = (BOT / "extension_content_template.js").read_text(encoding="utf-8")
content = (template.replace("/*__COLLECT__*/", autofill.COLLECT_JS.strip())
                   .replace("/*__BUTTONS__*/", autofill.BUTTONS_JS.strip()))
(EXT / "content.js").write_text(content, encoding="utf-8")
print("Extension built in", EXT)
