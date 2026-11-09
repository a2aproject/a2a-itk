import type { Transport } from "../acts/types.ts";
import type { CellStatus } from "../itk/types.ts";
import type { SdkTarget } from "../shared/types.ts";

export type ItkProtocol = "jsonrpc" | "grpc" | "http_json";

export interface ProtocolCounts {
  passed: number;
  total: number;
  status: CellStatus;
}

export interface ItkSdkSummary {
  timestamp: string;
  commit_sha: string;
  allPassed: boolean;
  passed: number;
  total: number;
  byProtocol: Record<ItkProtocol, ProtocolCounts>;
}

export interface TransportSummary {
  conformant: boolean;
  passed: number;
  total: number;
  mustPassed: number;
  mustTotal: number;
}

export interface ActsSdkSummary {
  timestamp: string;
  commit_sha: string;
  conformant: boolean;
  passed: number;
  total: number;
  mustPassed: number;
  mustTotal: number;
  byTransport: Partial<Record<Transport, TransportSummary>>;
}

export interface SdkOverview {
  sdk: SdkTarget;
  itk?: ItkSdkSummary;
  acts?: ActsSdkSummary;
}
