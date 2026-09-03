interface AstrBotPluginPageBridge {
  ready(): Promise<void>;
  apiGet(path: string): Promise<unknown>;
  apiPost(
    path: string,
    payload: Readonly<Record<string, unknown>>,
  ): Promise<unknown>;
}

interface DashenSigner {
  gen_sign(body: string): unknown;
  get_version?: () => unknown;
}

interface DashenSignerNamespace {
  default(): Promise<DashenSigner>;
}

interface DashenSignerLocalData {
  csrf: string;
  cst?: string;
  time_diff?: string;
}

interface Window {
  AstrBotPluginPage: AstrBotPluginPageBridge;
  sig?: DashenSignerNamespace;
  __dashenSigWasmUrl?: string;
  __dashenSigLocalData?: DashenSignerLocalData;
}
