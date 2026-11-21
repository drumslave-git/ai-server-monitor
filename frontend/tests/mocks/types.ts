import type { paths } from "../../src/api-types";

/** The JSON body of a 200 (or 201) answer of an endpoint, straight from the generated API types:
 *  a fixture typed with it stops compiling when the API changes. */
export type ApiResponse<P extends keyof paths, M extends keyof paths[P] = "get" extends keyof paths[P] ? "get" : never> = paths[P][M] extends {
  responses: infer R;
}
  ? R extends { 200: { content: { "application/json": infer B } } }
    ? B
    : R extends { 201: { content: { "application/json": infer B } } }
      ? B
      : never
  : never;

/** The JSON body a request of an endpoint carries. */
export type ApiRequest<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody: { content: { "application/json": infer B } };
}
  ? B
  : never;
