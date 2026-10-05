/**
 * Short-lived voice state: sessions, order intents, purchase authorizations,
 * payment authorizations and idempotency records. Nothing here is a system
 * of record — customers, prices and orders stay in GIMME's core services.
 *
 * The interfaces are the shape a Redis implementation needs (GET / SET EX /
 * SET NX EX / DEL, and a lock built on SET NX PX). The in-memory
 * implementation is correct for a single instance; run more than one
 * instance only with a shared implementation behind these interfaces.
 */

export interface KeyValueStore<T> {
  get(key: string): Promise<T | undefined>;
  set(key: string, value: T, ttlSeconds: number): Promise<void>;
  /** Atomic create-if-absent. Returns false if the key already exists. */
  setIfAbsent(key: string, value: T, ttlSeconds: number): Promise<boolean>;
  delete(key: string): Promise<void>;
}

export interface LockManager {
  /** Run fn while holding an exclusive lock on key. Serialises concurrent work on one order intent. */
  withLock<R>(key: string, fn: () => Promise<R>): Promise<R>;
}

export class MemoryStore<T> implements KeyValueStore<T> {
  private readonly data = new Map<string, { value: T; expiresAt: number }>();

  constructor(private readonly now: () => number = Date.now) {}

  async get(key: string): Promise<T | undefined> {
    const entry = this.data.get(key);
    if (!entry) return undefined;
    if (entry.expiresAt <= this.now()) {
      this.data.delete(key);
      return undefined;
    }
    return structuredClone(entry.value);
  }

  async set(key: string, value: T, ttlSeconds: number): Promise<void> {
    this.data.set(key, { value: structuredClone(value), expiresAt: this.now() + ttlSeconds * 1000 });
  }

  async setIfAbsent(key: string, value: T, ttlSeconds: number): Promise<boolean> {
    if ((await this.get(key)) !== undefined) return false;
    await this.set(key, value, ttlSeconds);
    return true;
  }

  async delete(key: string): Promise<void> {
    this.data.delete(key);
  }

  /** Values not yet expired. Used by the sandbox and by tests, not by request paths. */
  async values(): Promise<T[]> {
    const out: T[] = [];
    for (const key of this.data.keys()) {
      const v = await this.get(key);
      if (v !== undefined) out.push(v);
    }
    return out;
  }
}

export class MemoryLockManager implements LockManager {
  private readonly tails = new Map<string, Promise<unknown>>();

  async withLock<R>(key: string, fn: () => Promise<R>): Promise<R> {
    const prev = this.tails.get(key) ?? Promise.resolve();
    let release!: () => void;
    const mine = new Promise<void>((r) => (release = r));
    const tail = prev.then(() => mine);
    this.tails.set(key, tail);
    await prev;
    try {
      return await fn();
    } finally {
      release();
      if (this.tails.get(key) === tail) this.tails.delete(key);
    }
  }
}
