// Fetch OpenAPI schema from running backend and regenerate api-types.ts
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const API_URL = process.env.API_URL || "http://localhost:8000";
const OPENAPI_FILE = process.env.OPENAPI_FILE;

async function main() {
  let schema;
  let source;
  if (OPENAPI_FILE) {
    schema = JSON.parse(readFileSync(OPENAPI_FILE, "utf-8"));
    source = "FastAPI application export";
  } else {
    const res = await fetch(`${API_URL}/openapi.json`);
    if (!res.ok) throw new Error(`Failed to fetch OpenAPI schema: ${res.status}`);
    schema = await res.json();
    source = `${API_URL}/openapi.json`;
  }

  const schemas = schema.components?.schemas ?? {};
  const lines = [
    "// Auto-generated from FastAPI /openapi.json; run `npm run generate-api-types` to refresh",
    `// Source: ${source}`,
    "",
  ];

  for (const [name, def] of Object.entries(schemas)) {
    if (def.enum) {
      const values = def.enum.map((v) => `"${v}"`).join(" | ");
      lines.push(`export type ${name} = ${values};`);
      lines.push("");
    } else if (def.oneOf || def.anyOf || def.allOf) {
      lines.push(`export type ${name} = ${openApiTypeToTs(def, schemas)};`);
      lines.push("");
    } else if (def.properties) {
      lines.push(`export interface ${name} {`);
      const required = new Set(def.required ?? []);
      for (const [prop, propDef] of Object.entries(def.properties)) {
        const optional = required.has(prop) ? "" : "?";
        const tsType = openApiTypeToTs(propDef, schemas);
        lines.push(`  ${prop}${optional}: ${tsType};`);
      }
      lines.push("}");
      lines.push("");
    }
  }

  writeFileSync(
    fileURLToPath(new URL("../src/api-types.ts", import.meta.url)),
    lines.join("\n"),
    "utf-8"
  );
  console.log("Generated frontend/src/api-types.ts");
}

function openApiTypeToTs(schema, schemas) {
  if (schema.$ref) {
    const refName = schema.$ref.split("/").pop();
    return refName || "unknown";
  }
  if (Object.hasOwn(schema, "const")) return JSON.stringify(schema.const);
  if (schema.enum) return schema.enum.map((v) => `"${v}"`).join(" | ");
  if (schema.oneOf) {
    const types = schema.oneOf.map((s) => openApiTypeToTs(s, schemas));
    return [...new Set(types)].join(" | ");
  }
  if (schema.anyOf) {
    const types = schema.anyOf.map((s) => openApiTypeToTs(s, schemas));
    return [...new Set(types)].join(" | ");
  }
  if (schema.allOf) {
    return schema.allOf.map((s) => openApiTypeToTs(s, schemas)).join(" & ");
  }
  const typeMap = {
    string: "string",
    integer: "number",
    number: "number",
    boolean: "boolean",
    null: "null",
  };
  let result = typeMap[schema.type] ?? "unknown";
  if (schema.nullable) result += " | null";
  if (schema.type === "array") {
    const itemType = openApiTypeToTs(schema.items ?? { type: "string" }, schemas);
    result = itemType.includes(" | ") || itemType.includes(" & ")
      ? `(${itemType})[]`
      : `${itemType}[]`;
  }
  if (schema.type === "object" || schema.properties || schema.additionalProperties) {
    if (schema.properties) {
      const required = new Set(schema.required ?? []);
      const fields = Object.entries(schema.properties).map(([name, value]) => {
        const key = /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name) ? name : JSON.stringify(name);
        return `${key}${required.has(name) ? "" : "?"}: ${openApiTypeToTs(value, schemas)}`;
      });
      result = `{ ${fields.join("; ")} }`;
    } else if (typeof schema.additionalProperties === "object") {
      result = `{ [key: string]: ${openApiTypeToTs(schema.additionalProperties, schemas)} }`;
    } else {
      result = "Record<string, unknown>";
    }
  }
  return result;
}

main().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
