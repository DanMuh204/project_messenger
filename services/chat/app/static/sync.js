// The browser and node tests use this same implementation.
export class Timeline {
  constructor() {
    this.messages = new Map();
    this.bySeq = new Map();
    this.cursor = 0;
    this.initialized = false;
    this.running = null;
    this.wanted = 0;
    this.hasOlder = false;
  }
  ingest(message) {
    if (!Number.isSafeInteger(message.seq) || message.seq < 1) throw new Error('Некорректный номер сообщения');
    const previous = this.messages.get(message.message_id);
    const atSeq = this.bySeq.get(message.seq);
    if ((previous && previous.seq !== message.seq) || (atSeq && atSeq !== message.message_id)) {
      throw new Error('Конфликт последовательности сообщений');
    }
    if (!previous && !this.initialized && this.messages.size >= 1000) {
      // Before the first snapshot, HTTP can safely restore discarded live events.
      this.messages.clear(); this.bySeq.clear();
    }
    this.messages.set(message.message_id, message);
    this.bySeq.set(message.seq, message.message_id);
    if (this.initialized) this.advance();
    const buffered = [...this.bySeq.keys()].filter(seq => seq > this.cursor).length;
    if (buffered > 1000) throw new Error('Переполнен буфер синхронизации');
  }
  advance() {
    while (this.bySeq.has(this.cursor + 1)) this.cursor++;
  }
  initialize(page) {
    if (this.initialized) return;
    const items = [...page.items].sort((a,b) => a.seq-b.seq);
    for (let i=1;i<items.length;i++) {
      if (items[i].seq !== items[i-1].seq+1) throw new Error('Неполный снимок истории');
    }
    for (const item of items) this.ingest(item);
    this.cursor = items.length ? items.at(-1).seq : 0;
    this.initialized = true;
    this.hasOlder = page.has_more;
    this.advance();
  }
  older(page) {
    for (const item of page.items) this.ingest(item);
    this.hasOlder = page.has_more;
  }
  get oldest() { return this.bySeq.size ? [...this.bySeq.keys()].reduce((a,b) => Math.min(a,b)) : null; }
  get latest() { return this.bySeq.size ? [...this.bySeq.keys()].reduce((a,b) => Math.max(a,b)) : 0; }
  ordered() { return [...this.messages.values()].sort((a,b) => a.seq-b.seq); }
  async syncTo(target, fetchPage) {
    this.wanted = Math.max(target, this.wanted);
    if (!this.initialized) throw new Error('Сначала нужен снимок истории');
    if (this.running) return this.running;
    this.running = (async () => {
      while (this.cursor < this.wanted) {
        const before = this.cursor;
        const page = await fetchPage(before);
        for (const message of page.items) this.ingest(message);
        if (this.cursor <= before) throw new Error('История ещё не синхронизирована');
      }
    })();
    try { await this.running; } finally { this.running = null; }
  }
}
