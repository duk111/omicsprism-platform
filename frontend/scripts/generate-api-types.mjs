// Fetch OpenAPI schema from running backend and regenerate api-types.ts
const API_URL = process.env.API_URL || "http://localhost:8000";

async function main() {
  const res = await fetch(`${API_URL}/openapi.json`);
  if (!res.ok) throw new Error(`Failed to fetch OpenAPI schema: ${res.status}`);
  const schema = await res.json();

  const schemas = schema.components?.schemas ?? {};
  const lines = [
    "// Auto-generated from FastAPI /openapi.json — DO NOT EDIT",
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
        const tsType = openApiTypeToTs(propDef);
        lines.push(`  ${prop}${optional}: ${tsType};`);
      }
      lines.push("}");
      lines.push("");
    }
  }

  const fs = await import("fs");
  fs.writeFileSync(
    new URL("../src/api-types.ts", import.meta.url).pathname,
    lines.join("\n"),
    "utf-8"
  );
  console.log("Generated frontend/src/api-types.ts");
}

function openApiTypeToTs(schema) {
  if (schema.enum) return schema.enum.map((v) => `"${v}"`).join(" | ");
  if (schema.anyOf) {
    return schema.anyOf.map(openApiTypeToTs).join(" | ") + (schema.nullable ? " | null" : "");
  }
  const typeMap = {
    string: "string",
    integer: "number",
    number: "number",
    boolean: "boolean",
  };
  if (schema.type === "array") {
    const itemType = openApiTypeToTs(schema.items ?? { type: "string" });
    return `${itemType}[]`;
  }
  return typeMap[schema.type] ?? "unknown";
}

main().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
