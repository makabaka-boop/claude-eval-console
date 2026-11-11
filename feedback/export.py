"""Local export uses explicit text cells, not the historical 26-column GSB map."""
import io
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from . import policy,store,review

COLUMNS=('User Prompt','所属领域','子方向','任务类型','任务难度','语言/框架','Harness','Harness 版本','操作系统',
         '环境可复现等级','初始环境快照','SessionID','TurnID/PromptID','轨迹文件','产物快照','运行录屏',
         '交付完整性','Feedback','其他问题','备注','prompt附件')
NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def values(h,run_id):
    checked=review.preflight(h,run_id,deep=True)
    if not checked['ready_for_local_export']:raise h.WorkflowError('导出前请补齐：'+'；'.join(checked['issues']))
    r=store.run(h,run_id);t=store.task(h,r['task_id']);c=r['config']
    return [t['prompt'],t['domain'],t['subdirection'],t['task_type'],t['difficulty'],t['languages'],
            c.get('harness',''),c.get('harness_version',''),c.get('operating_system',''),t['reproducibility'],
            t['initial_snapshot'],r['first_session_id'],r['first_turn_id'],
            r['trace_url'] or f"本地轨迹：{Path(r['trace_path']).name}",
            f"{t['repo_url']}/commit/{r['artifact_sha']}",
            r['recording_url'] or f"本地录屏：{Path(r['recording_path']).name}",
            str(r['delivery_score']),r['feedback'],r['other_issues'],r['notes'],
            '\n'.join(f"{a['name']} (SHA-256: {a['sha256']})" for a in t['attachments'])]


def rows(h,ids):
    if not isinstance(ids,list) or not ids or len(ids)>200 or any(not isinstance(i,str) or not re.fullmatch('[a-f0-9]{12}',i) for i in ids) or len(set(ids))!=len(ids):
        raise h.WorkflowError('请选择 1–200 条不重复的执行记录')
    return [values(h,run_id) for run_id in ids]


def bundle(h,ids):
    data=rows(h,ids)
    return json.dumps({'policy_version':policy.VERSION,'format':'local-review-export',
                       'notice':'正式模板未配置；本地文件标记不是评测方可下载的外部链接',
                       'columns':COLUMNS,'rows':data,
                       'samples':[{'run_id':i,'analysis_model':store.run(h,i)['analysis_model'],
                                   'analysis_effort':store.run(h,i)['analysis_effort'],
                                   'preflight':review.preflight(h,i)} for i in ids]},ensure_ascii=False,indent=2).encode()


def column_name(index):
    text=''
    while index:
        index,rest=divmod(index-1,26);text=chr(65+rest)+text
    return text


def workbook(h,ids):
    data=[list(COLUMNS),*rows(h,ids)]
    if any(re.search('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]',str(value)) for row in data for value in row):
        raise h.WorkflowError('文本含 Excel XML 不支持的字符，未修改原文；请使用 JSON 导出')
    if any(len(str(value).encode('utf-16-le'))//2>32767 for row in data for value in row):
        raise h.WorkflowError('文本超过 Excel 单元格上限，未截断原文；请使用 JSON 导出')
    worksheet=ET.Element('worksheet',xmlns=NS)
    views=ET.SubElement(worksheet,'sheetViews');view=ET.SubElement(views,'sheetView',workbookViewId='0')
    ET.SubElement(view,'pane',ySplit='1',topLeftCell='A2',activePane='bottomLeft',state='frozen')
    columns=ET.SubElement(worksheet,'cols')
    for i in range(1,len(COLUMNS)+1):
        ET.SubElement(columns,'col',min=str(i),max=str(i),width='65' if i in {1,18,19,20} else '28',customWidth='1')
    sheet_data=ET.SubElement(worksheet,'sheetData')
    for number,row in enumerate(data,1):
        element=ET.SubElement(sheet_data,'row',r=str(number),ht='40' if number==1 else '72',customHeight='1')
        for index,value in enumerate(row,1):
            cell=ET.SubElement(element,'c',r=f'{column_name(index)}{number}',t='inlineStr',s='1' if number==1 else '0')
            inline=ET.SubElement(cell,'is');text=ET.SubElement(inline,'t',{'{http://www.w3.org/XML/1998/namespace}space':'preserve'})
            text.text=str(value)
    ET.SubElement(worksheet,'autoFilter',ref=f'A1:{column_name(len(COLUMNS))}{len(data)}')
    files={
      '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
      '_rels/.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
      'xl/workbook.xml':'<workbook xmlns="'+NS+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="领域专项 Feedback" sheetId="1" r:id="rId1"/></sheets></workbook>',
      'xl/_rels/workbook.xml.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
      'xl/styles.xml':'<styleSheet xmlns="'+NS+'"><fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Arial"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF315F55"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
      'xl/worksheets/sheet1.xml':ET.tostring(worksheet,encoding='utf-8',xml_declaration=True).replace(b'\r',b'&#13;')}
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,content in files.items():archive.writestr(name,content)
    return output.getvalue()
