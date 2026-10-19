#!/usr/bin/env node
/**
 * zstd-jsonl — 解码 DSH 的多帧 session.jsonl.zstd 并输出 JSONL 文本。
 *
 * DSH 以流式追加方式写会话文件：每次 flush 追加一个独立 zstd 帧，
 * 因此文件是若干帧的拼接，node:zlib 的单次 decode 只会解出第一帧。
 *
 * 本脚本直接解析 zstd 帧头计算每个帧的精确长度（O(n)，不做逐帧试探
 * 解码——41MB 的真实会话文件用试探法会退化为 O(n²) 并超时），逐帧
 * 解码后拼接输出。遇到无法解析的帧头时退化为"魔数 + 输出等值比较"
 * 的保守探测；正在写入中的尾部残帧被静默截断，不算错误。
 *
 * 超过 --max-bytes 时输出「头部 64KB + 尾部余量」：session 头事件在
 * 文件开头（会话 id/cwd），最近的对话在文件末尾，两头都要保住。
 *
 * 用法：node zstd-jsonl.mjs <session.jsonl.zstd> [--max-bytes N]
 * 退出码：0 正常（可能为空）；2 用法错误；3 解码失败（损坏文件）。
 */

import { readFileSync } from "node:fs"
import { realpathSync } from "node:fs"
import { pathToFileURL } from "node:url"
import zlib from "node:zlib"

const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])
const SKIPPABLE_MIN = 0x184d2a50
const SKIPPABLE_MAX = 0x184d2a5f
const HEAD_KEEP_BYTES = 64 * 1024

/** 解析一个标准 zstd 帧的完整长度（含魔数与可选校验和）。 */
function frameLength(buffer, start) {
  let offset = start + 4
  if (offset >= buffer.length) return -1
  const descriptor = buffer[offset]
  offset += 1
  const fcsFlag = descriptor >>> 6
  const singleSegment = (descriptor & 0x20) !== 0
  const hasChecksum = (descriptor & 0x04) !== 0
  const dictIdFlag = descriptor & 0x03
  const dictIdSize = [0, 1, 2, 4][dictIdFlag] ?? -1
  if (dictIdSize < 0 || (descriptor & 0x08) !== 0) return -1 // 保留位非法
  if (!singleSegment) {
    offset += 1 // window descriptor
  }
  offset += dictIdSize
  let fcsSize = 0
  if (fcsFlag === 0) {
    fcsSize = singleSegment ? 1 : 0
  } else {
    fcsSize = [0, 2, 4, 8][fcsFlag] ?? -1
  }
  if (fcsSize < 0 || offset + fcsSize > buffer.length) return -1
  offset += fcsSize
  // 数据块：3 字节头（last | type(2bit) | size(21bit)），直到 last 块。
  // 块类型：0=Raw（后跟 size 字节）、1=RLE（后跟 1 字节，size 是重复
  // 次数而非字节数！）、2=Compressed（后跟 size 字节）、3=保留（非法）。
  for (let guard = 0; guard < 1_000_000; guard += 1) {
    if (offset + 3 > buffer.length) return -1
    const header = buffer.readUIntLE(offset, 3)
    const blockType = (header >>> 1) & 0x03
    const blockSize = header >>> 3
    const last = (header & 0x1) !== 0
    if (blockType === 0x03) return -1 // 保留块类型
    offset += 3
    const payload = blockType === 0x01 ? 1 : blockSize
    if (offset + payload > buffer.length) return -1
    offset += payload
    if (last) break
  }
  if (hasChecksum) offset += 4
  if (offset > buffer.length) return -1
  return offset - start
}

function magicOffsets(buffer) {
  const offsets = []
  let index = 0
  while ((index = buffer.indexOf(MAGIC, index)) !== -1) {
    offsets.push(index)
    index += 1
  }
  return offsets
}

/** 保守回退：对解析失败的位置用输出等值比较探测帧边界。 */
function probeFrameEnd(buffer, position, candidates, fullOutput) {
  for (const candidate of candidates.filter((value) => value > position).sort((a, b) => a - b).slice(0, 64)) {
    try {
      if (zlib.zstdDecompressSync(buffer.subarray(position, candidate)).equals(fullOutput)) {
        return candidate
      }
    } catch {
      // 候选切在帧内部：继续
    }
  }
  return buffer.length
}

export function decompressAll(buffer, maxBytes = 32 * 1024 * 1024) {
  if (buffer.length === 0) return buffer // 会话刚创建：合法的空文件
  const firstMagic = buffer.readUInt32LE(0)
  const startsStandard = buffer.indexOf(MAGIC, 0) === 0
  const startsSkippable = firstMagic >= SKIPPABLE_MIN && firstMagic <= SKIPPABLE_MAX
  if (!startsStandard && !startsSkippable) {
    // 非空且不以 zstd 帧开头：真正的损坏文件（而非写入中的残帧）。
    throw new Error("input is not a zstd frame stream")
  }
  const candidates = magicOffsets(buffer)
  const parts = []
  let position = 0
  let total = 0
  while (position < buffer.length && total < maxBytes) {
    // 可跳过帧（magic 0x184D2A5x + 4 字节长度）：直接跳过。
    if (position + 8 <= buffer.length) {
      const magic = buffer.readUInt32LE(position)
      if (magic >= SKIPPABLE_MIN && magic <= SKIPPABLE_MAX) {
        const size = buffer.readUInt32LE(position + 4)
        position += 8 + size
        continue
      }
    }
    if (buffer.indexOf(MAGIC, position) !== position) break // 尾部残帧
    let full
    try {
      full = zlib.zstdDecompressSync(buffer.subarray(position))
    } catch (error) {
      if (position === 0) throw error // 首帧即损坏
      break // 写入中的尾部不完整帧：保留已解码内容
    }
    const parsed = frameLength(buffer, position)
    const end = parsed > 0 ? position + parsed : probeFrameEnd(buffer, position, candidates, full)
    if (end <= position) break
    parts.push(full)
    total += full.length
    position = end
  }
  const joined = Buffer.concat(parts)
  if (joined.length <= maxBytes) return joined
  // 超预算：保留头部（session 头事件）+ 尾部（最近的对话轮次）。
  const tail = maxBytes - HEAD_KEEP_BYTES
  return Buffer.concat([joined.subarray(0, HEAD_KEEP_BYTES), joined.subarray(joined.length - tail)])
}

function isMainEntry() {
  try {
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href
  } catch {
    return false
  }
}

if (isMainEntry()) {
  const [, , target, ...rest] = process.argv
  if (!target) {
    process.stderr.write("usage: node zstd-jsonl.mjs <session.jsonl.zstd> [--max-bytes N]\n")
    process.exit(2)
  }
  const maxFlag = rest.indexOf("--max-bytes")
  const maxBytes = maxFlag >= 0 ? Number(rest[maxFlag + 1]) || 32 * 1024 * 1024 : 32 * 1024 * 1024
  try {
    const output = decompressAll(readFileSync(target), maxBytes)
    process.stdout.write(output)
  } catch (error) {
    process.stderr.write(`zstd-jsonl: ${target} 解码失败: ${String(error)}\n`)
    process.exit(3)
  }
}
