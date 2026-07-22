import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

export async function verifyContracts() {
  const manifest = JSON.parse(
    await readFile(resolve(packageRoot, "manifest.json"), "utf8"),
  );

  if (!/^[1-9]\d*\.\d+\.\d+$/.test(manifest.current_version)) {
    throw new Error("manifest.current_version must be semantic versioning");
  }

  for (const relativePath of manifest.schemas) {
    const schema = JSON.parse(
      await readFile(resolve(packageRoot, relativePath), "utf8"),
    );
    if (schema.$schema !== "https://json-schema.org/draft/2020-12/schema") {
      throw new Error(`${relativePath}: expected JSON Schema draft 2020-12`);
    }
    if (!schema.$id?.includes("/v1/")) {
      throw new Error(`${relativePath}: schema identifier must include /v1/`);
    }
    if (schema.properties?.schema_version?.const !== manifest.current_version) {
      throw new Error(`${relativePath}: schema_version differs from the manifest`);
    }
    if (schema.additionalProperties !== false) {
      throw new Error(`${relativePath}: additional properties must fail closed`);
    }
  }

  return manifest.schemas.length;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const count = await verifyContracts();
  console.log(`Verified ${count} versioned contracts`);
}
