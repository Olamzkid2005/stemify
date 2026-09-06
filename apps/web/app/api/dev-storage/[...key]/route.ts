import { NextResponse } from "next/server";

import { getStorage } from "@/lib/storage";
import { FakeStorage } from "@/lib/storage/fake";

/**
 * Dev-only storage endpoint backing the FakeStorage adapter so the browser
 * upload flow works without real object-storage credentials. Returns 404
 * whenever real storage is configured (the fake is never selected then).
 */
export async function PUT(
  request: Request,
  { params }: { params: Promise<{ key: string[] }> },
) {
  const storage = getStorage();
  if (!(storage instanceof FakeStorage)) {
    return NextResponse.json({ error: "not_available" }, { status: 404 });
  }
  const { key } = await params;
  const objectKey = key.map(decodeURIComponent).join("/");
  if (!objectKey.startsWith("sources/")) {
    return NextResponse.json({ error: "forbidden_key" }, { status: 403 });
  }
  const data = Buffer.from(await request.arrayBuffer());
  if (data.length === 0) {
    return NextResponse.json({ error: "empty_body" }, { status: 400 });
  }
  storage.put(objectKey, data);
  return new NextResponse(null, { status: 200, headers: { etag: `"dev-${data.length}"` } });
}
