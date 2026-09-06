declare module "node:sqlite" {
  export class DatabaseSync {
    constructor(location: string);
    exec(sql: string): void;
    prepare(sql: string): StatementSync;
    close(): void;
  }

  export class StatementSync {
    run(...params: unknown[]): { changes: bigint; lastInsertRowid: bigint };
    get<T = Record<string, unknown>>(...params: unknown[]): T | undefined;
    all<T = Record<string, unknown>>(...params: unknown[]): T[];
  }
}
