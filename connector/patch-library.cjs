// Adapted from EnzoSocial (GPL-3.0): modern WhatsApp MsgKey has toString(),
// but no _serialized. Look up the exact generated key after sending.
const fs = require('node:fs');
const file = require.resolve('whatsapp-web.js/src/util/Injected/Utils.js');
const source = fs.readFileSync(file, 'utf8');
const before = '.Msg.get(newMsgKey._serialized)';
if (source.split(before).length !== 2) throw Error('Review WhatsApp send-key patch for this library version');
const model = 'const msg = message.serialize();';
if (source.split(model).length !== 2) throw Error('Review WhatsApp message-key serialization patch');
fs.writeFileSync(file, source.replace(before, '.Msg.get(newMsgKey)').replace(model,
  model + '\n        msg.id = {...msg.id, _serialized: String(message.id)};'));
