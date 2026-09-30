// Client-generated ids for messages and conversations. The server accepts
// `^[A-Za-z0-9_-]{8,64}$` and rejects an id reused in another conversation, so
// ids are long and come from the platform's cryptographic generator.

const ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

export const ID_PATTERN = /^[A-Za-z0-9_-]{8,64}$/;

export function randomId(prefix: string, length = 22): string {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  let out = prefix;
  // 62 symbols from bytes: rejection sampling keeps the distribution even.
  let i = 0;
  while (out.length < prefix.length + length) {
    if (i >= bytes.length) {
      crypto.getRandomValues(bytes);
      i = 0;
    }
    const b = bytes[i++];
    if (b < 248) out += ALPHABET[b % 62];
  }
  return out;
}

export const newMessageId = () => randomId("m_");
export const newConversationId = () => randomId("c_");

/**
 * assistant-ui's local runtime names messages with short ids of its own. The
 * server needs ids in its format, so each local id is paired with a server id
 * the first time it is sent. Messages loaded from the server already carry
 * server ids and map to themselves.
 */
export class IdMap {
  private toServerIds = new Map<string, string>();
  private persisted = new Set<string>();

  /** The server id for a local (or already server) id. */
  toServer(localId: string): string {
    const known = this.toServerIds.get(localId);
    if (known) return known;
    if (this.persisted.has(localId)) return localId;
    const id = ID_PATTERN.test(localId) && localId.startsWith("m_") ? localId : newMessageId();
    this.toServerIds.set(localId, id);
    return id;
  }

  /** Use this server id for that local id (the server chose its own). */
  bind(localId: string, serverId: string) {
    this.toServerIds.set(localId, serverId);
  }

  /** The server has stored this message. */
  markPersisted(serverId: string) {
    this.persisted.add(serverId);
  }

  isPersisted(serverId: string): boolean {
    return this.persisted.has(serverId);
  }
}
