import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [, , input, output] = process.argv;
const { columns, rows } = JSON.parse(await fs.readFile(input, "utf8"));
if (columns.length !== 26 || !rows.length || rows.some(r => r.length !== 26)) {
  throw new Error("Pair-wise export requires exactly 26 columns and nonempty rows");
}
if (rows.flat().some(v => String(v ?? "").length > 32767)) {
  throw new Error("字段超出 Excel 单元格 32767 字符上限；为保留原文，已停止导出");
}
const wb = Workbook.create();
const sheet = wb.worksheets.add("Pair-wise GSB");
sheet.showGridLines = false;
const last = rows.length + 1;
const all = sheet.getRange(`A1:Z${last}`);
all.setNumberFormat("@");
const literal = v => typeof v === "string" && /^[=+@-]/.test(v) ? `'${v}` : v;
all.values = [columns, ...rows.map(r => r.map(literal))];
all.format.font = { name: "Arial", size: 11, color: "#24323A" };
all.format.verticalAlignment = "top";
all.format.wrapText = true;
sheet.getRange("A1:Z1").format = {
  fill: "#315F55", font: { name: "Arial", size: 11, bold: true, color: "#FFFFFF" },
  wrapText: true, rowHeight: 32, verticalAlignment: "center",
};
for (let i = 0; i < 26; i++) {
  const col = String.fromCharCode(65 + i);
  sheet.getRange(`${col}:${col}`).format.columnWidth = [0,14,21,24].includes(i) ? 65 :
    [8,10,11,12,17,18,19,25].includes(i) ? 44 : 23;
}
// The renderer's automatic row sizing underestimates CJK and explicit newlines.
// Size from every cell (including long URLs) and respect Excel's row limit.
rows.forEach((row, index) => {
  const lines = Math.max(...row.map((value, i) => {
    const width = [0,14,21,24].includes(i) ? 65 : [8,10,11,12,17,18,19,25].includes(i) ? 44 : 23;
    return String(value ?? "").split("\n").reduce((sum, line) =>
      sum + Math.max(1, Math.ceil([...line].reduce((n,c) => n + (c.charCodeAt(0)>255 ? 2 : 1),0)/(width-3))),0);
  }));
  sheet.getRange(`A${index+2}:Z${index+2}`).format.rowHeight = Math.min(409, Math.max(48,lines*16+8));
});
sheet.freezePanes.freezeRows(1);
sheet.tables.add(`A1:Z${last}`, true, "PairwiseGSB").showFilterButton = true;
if (process.env.PAIRWISE_EXPORT_QA === "1") {
  console.log((await wb.inspect({kind:"table", range:"'Pair-wise GSB'!A1:Z2", tableMaxRows:2, tableMaxCols:26, maxChars:3000})).ndjson);
  console.log((await wb.inspect({kind:"match", searchTerm:"#REF!|#DIV/0!|#VALUE!|#NAME\\?|#SPILL!", options:{useRegex:true,maxResults:10}})).ndjson);
  const preview = await wb.render({sheetName:"Pair-wise GSB",range:"A1:D2",scale:1,format:"png"});
  await fs.writeFile(`${output}.png`, new Uint8Array(await preview.arrayBuffer()));
}
await (await SpreadsheetFile.exportXlsx(wb)).save(output);
