// Fetch OpenAPI schema from running backend and regenerate api-types.ts
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const API_URL = process.env.API_URL || "http://localhost:8000";

async function main() {
  const res = await fetch(`${API_URL}/openapi.json`);
  if (!res.ok) throw new Error(`Failed to fetch OpenAPI schema: ${res.status}`);
  const schema = await res.json();

  const schemas = schema.components?.schemas ?? {};
  const lines = [
    "// Auto-generated from FastAPI /openapi.json — run `npm run generate-api-types` to refresh",
    `// Source: ${API_URL}/openapi.json`,
    `// Generated: ${new Date().toISOString()}`,
    "",
  ];

  for (const [name, def] of Object.entries(schemas)) {
    if (def.enum) {
      const values = def.enum.map((v) => `"${v}"`).join(" | ");
      lines.push(`export type ${name} = ${values};`);
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
  if (schema.enum) return schema.enum.map((v) => `"${v}"`).join(" | ");
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
  };
  let result = typeMap[schema.type] ?? "unknown";
  if (schema.nullable) result += " | null";
  if (schema.type === "array") {
    const itemType = openApiTypeToTs(schema.items ?? { type: "string" }, schemas);
    result = `${itemType}[]`;
  }
  return result;
}

main().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
