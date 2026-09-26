/** A random password: 20 characters with lower and upper case, digits and
 *  symbols, so it also satisfies Open WebUI's optional strength rule. Made in
 *  the browser; the server never generates or returns one. */
export function generatePassword(length = 20): string {
  const sets = ["abcdefghijkmnopqrstuvwxyz", "ABCDEFGHJKLMNPQRSTUVWXYZ", "23456789", "-_.!@#%+="];
  const all = sets.join("");
  const random = new Uint32Array(length + sets.length);
  crypto.getRandomValues(random);
  const chars = sets.map((set, i) => set[random[i] % set.length]);
  for (let i = sets.length; i < length; i++) chars.push(all[random[i] % all.length]);
  // Shuffle so the guaranteed characters are not always first.
  const order = new Uint32Array(chars.length);
  crypto.getRandomValues(order);
  for (let i = chars.length - 1; i > 0; i--) {
    const j = order[i] % (i + 1);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join("");
}

/** The same minimum the server applies. */
export function passwordProblem(password: string): string | null {
  if (!password) return "Enter a password or generate one.";
  if (password.length < 8) return "Use at least 8 characters.";
  if (new TextEncoder().encode(password).length > 72) return "Use at most 72 bytes.";
  return null;
}
