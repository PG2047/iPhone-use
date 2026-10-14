from pathlib import Path
import copy
import shutil
import textwrap
from html import escape
from lxml import html
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, Color, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph
from reportlab.graphics.svgpath import SvgPath
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'output/pdf/iPhone-Use-图文使用手册.pdf'
OUT.parent.mkdir(parents=True, exist_ok=True)
doc=html.fromstring((ROOT/'docs/user-guide/index.html').read_text())
pdfmetrics.registerFont(TTFont('CJK','/System/Library/Fonts/STHeiti Light.ttc',subfontIndex=1))
pdfmetrics.registerFont(TTFont('CJKB','/System/Library/Fonts/STHeiti Medium.ttc',subfontIndex=1))
pdfmetrics.registerFontFamily('CJK',normal='CJK',bold='CJKB')
W,H=A4; M=42; WIDTH=W-2*M
INK='#183129'; GREEN='#16765b'; MUTED='#61736c'; LIGHT='#e8f4ee'; LINE='#dbe4de'
c=canvas.Canvas(str(OUT),pagesize=A4,pageCompression=1)
c.setTitle('iPhone Use 图文使用手册');c.setAuthor('本机使用手册');c.setSubject('本地修复版 0.3.13；连接、任务、认证、排错、Xcode、升级和备份恢复')
styles={n:ParagraphStyle(n,fontName='CJK',fontSize=s,leading=l,textColor=HexColor(col),wordWrap='CJK',spaceAfter=0) for n,s,l,col in [('body',11,18,INK),('small',9.2,15,MUTED),('title',23,31,INK),('h2',17,24,INK),('h3',12.5,20,INK),('prompt',10.5,17,INK)]}
y=H-M

def tx(e): return ''.join(e.itertext()).strip()
def rich(e):
    out=escape(e.text or '')
    for child in e:
        tag=child.tag
        inner=rich(child)
        if tag in ('strong','b'): out+='<b>'+inner+'</b>'
        elif tag=='br': out+='<br/>'
        elif tag=='button': pass
        else: out+=inner
        out+=escape(child.tail or '')
    return out.strip()
def para(text,style='body',x=M,width=WIDTH,space=10):
    global y
    p=Paragraph(text,styles[style]); _,h=p.wrap(width,1000)
    if y-h < 57: raise ValueError(f'Page overflow: {text[:80]} at {y}, height {h}')
    p.drawOn(c,x,y-h);y-=h+space

def box(text,bg=LIGHT,style='body'):
    global y
    p=Paragraph(text,styles[style]);_,h=p.wrap(WIDTH-28,1000)
    h+=24
    if y-h <57: raise ValueError('Box overflow')
    c.setFillColor(HexColor(bg));c.roundRect(M,y-h,WIDTH,h,8,fill=1,stroke=0)
    p.drawOn(c,M+14,y-h+12);y-=h+12

def heading(n,title):
    global y
    c.setFillColor(HexColor(GREEN));c.circle(M+12,y-12,12,stroke=0,fill=1)
    c.setFillColor(white);c.setFont('CJKB',12);c.drawCentredString(M+12,y-16,str(n))
    para(title,'h2',x=M+35,width=WIDTH-35,space=17)

def newpage(n,title):
    global y
    if n>1: c.showPage()
    y=H-M
    c.setFillColor(HexColor(MUTED));c.setFont('CJK',8.5)
    c.drawString(M,24,'iPhone Use · 本机修复版 0.3.13')
    c.drawRightString(W-M,24,f'{n} / 8')
    c.setStrokeColor(HexColor(LINE));c.line(M,42,W-M,42)
    if n>1:
        c.setFillColor(HexColor(GREEN));c.setFont('CJKB',9);c.drawString(M,y,'IPHONE USE / QUICK GUIDE');y-=28
    c.bookmarkPage(str(n));c.addOutlineEntry(title,str(n),0,False)

def table_rows(rows):
    global y
    for row in rows:
        cells=row.findall('td')
        left=Paragraph(rich(cells[0]),styles['prompt']);right=Paragraph(rich(cells[1]),styles['small'])
        lw=132;rw=WIDTH-lw-26;_,lh=left.wrap(lw,1000);_,rh=right.wrap(rw,1000);h=max(lh,rh)+16
        if y-h<57: raise ValueError('Table overflow')
        c.setFillColor(HexColor(LIGHT));c.rect(M,y-h,lw+12,h,fill=1,stroke=0)
        left.drawOn(c,M+7,y-8-lh);right.drawOn(c,M+lw+20,y-8-rh)
        c.setStrokeColor(HexColor(LINE));c.line(M,y-h,M+WIDTH,y-h);y-=h
    y-=14

def make_assets():
    """Reuse the HTML diagrams in the GitHub guide and PDF."""
    folder=ROOT/'docs/user-guide/images';folder.mkdir(exist_ok=True)
    diagrams={}
    for name,section in [('connect','start'),('screen','screen'),('xcode','xcode')]:
        el=copy.deepcopy(doc.get_element_by_id(section).xpath('.//svg')[0])
        el.set('xmlns','http://www.w3.org/2000/svg')
        if 'viewbox' in el.attrib:el.set('viewBox',el.attrib.pop('viewbox'))
        for e in el.iter('text'):
            cls=e.get('class','')
            e.set('font-family','sans-serif')
            if 'font-size' not in e.attrib:e.set('font-size','14' if cls=='diagram-small' else '18')
            if 'fill' not in e.attrib:e.set('fill',MUTED if cls=='diagram-small' else INK)
        text=html.tostring(el,encoding='unicode',method='xml')
        (folder/f'{name}.svg').write_text(text+'\n');diagrams[name]=el
    for name in ('upgrade','restore'):
        cards=doc.get_element_by_id(name).xpath('.//div[@class="step"]')
        parts=['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 225" role="img">']
        for i,card in enumerate(cards):
            x=5+226*i;mid=x+106
            parts.append(f'<rect x="{x}" y="10" width="212" height="200" rx="14" fill="{LIGHT}" stroke="{LINE}"/>')
            for yy,text,size,bold in [(39,tx(card.find('div')),14,False),(73,tx(card.find('b')),19,True)]:
                weight='700' if bold else '400'
                parts.append(f'<text x="{mid}" y="{yy}" text-anchor="middle" font-family="sans-serif" font-size="{size}" font-weight="{weight}" fill="{GREEN if not bold else INK}">{escape(text)}</text>')
            for j,line in enumerate(textwrap.wrap(tx(card.find('span')),width=12)):
                parts.append(f'<text x="{mid}" y="{105+j*24}" text-anchor="middle" font-family="sans-serif" font-size="15" fill="{MUTED}">{escape(line)}</text>')
            if i<3:parts.append(f'<path d="M{x+215} 105h9m-5-5 5 5-5 5" fill="none" stroke="{GREEN}" stroke-width="2"/>')
        parts.append('</svg>');text=''.join(parts)
        (folder/f'{name}.svg').write_text(text+'\n');diagrams[name]=html.fromstring(text)
    return diagrams

def parsecolor(v):
    if v in (None,'none'): return None
    return HexColor({'white':'#ffffff','var(--ink)':INK,'var(--muted)':MUTED}.get(v,v))
def svg_diagram(el):
    global y
    _,_,vw,vh=map(float,el.get('viewbox',el.get('viewBox')).split())
    sc=WIDTH/vw; height=vh*sc
    c.saveState();c.translate(M,y);c.scale(sc,-sc)
    for e in el:
        tag=e.tag.lower()
        if tag in ('title','desc'): continue
        cls=e.get('class','')
        fill=parsecolor(e.get('fill',MUTED if cls=='diagram-small' else INK if tag=='text' else '#000000'))
        stroke=parsecolor(e.get('stroke','none'))
        c.setLineWidth(float(e.get('stroke-width','1')))
        c.setDash([float(v) for v in e.get('stroke-dasharray','').split()] or [])
        c.setLineCap(1 if e.get('stroke-linecap')=='round' else 0)
        if fill: c.setFillColor(fill)
        if stroke: c.setStrokeColor(stroke)
        if tag=='rect':
            c.roundRect(float(e.get('x',0)),float(e.get('y',0)),float(e.get('width')),float(e.get('height')),float(e.get('rx',0)),fill=int(fill is not None),stroke=int(stroke is not None))
        elif tag=='circle':
            c.circle(float(e.get('cx')),float(e.get('cy')),float(e.get('r')),fill=int(fill is not None),stroke=int(stroke is not None))
        elif tag=='path':
            p=SvgPath(e.get('d'),fillColor=fill,strokeColor=stroke,strokeWidth=float(e.get('stroke-width',1)),strokeLineCap=1 if e.get('stroke-linecap')=='round' else 0)
            drawing=Drawing(vw,vh);drawing.add(p);renderPDF.draw(drawing,c,0,0)
        elif tag=='text':
            c.saveState();c.translate(float(e.get('x',0)),float(e.get('y',0)));c.scale(1,-1)
            size=float(e.get('font-size',14 if cls=='diagram-small' else 18));font='CJKB' if e.get('font-weight')=='700' else 'CJK'
            c.setFont(font,size)
            t=tx(e);align=e.get('text-anchor','start')
            if t=='↻':
                c.setStrokeColor(white);c.setLineWidth(1.8);c.arc(-7,0,7,14,startAng=40,extent=285);c.line(3,13,8,13);c.line(8,13,8,8)
            elif align=='middle': c.drawCentredString(0,0,t)
            elif align=='end': c.drawRightString(0,0,t)
            else:c.drawString(0,0,t)
            c.restoreState()
    c.restoreState();y-=height+8

diagrams=make_assets()
sec=lambda name:doc.get_element_by_id(name)
newpage(1,'开始使用')
para('IPHONE USE / QUICK GUIDE','small',space=7)
para('让 Codex 帮你操作 iPhone','title',space=12)
para('图文使用手册','h2',space=10)
para('适用：本机修复版 0.3.13 · 已完成安装与签名<br/>内容编写：2026-10-13 · 升级与恢复说明更新：2026-10-14','small',space=22)
heading(1,'接上手机，说出任务')
s=sec('start');svg_diagram(s.xpath('.//svg')[0])
para(rich(s.xpath('.//figcaption')[0]),'small')
for i,e in enumerate(s.xpath('.//ol/li'),1): para(f'<b>{i}.</b> '+rich(e),space=6)
box(rich(s.xpath('.//div[@class="prompt"]/p')[0]),style='prompt')
for e in s.xpath('./p'): para(rich(e),'small',space=10)

newpage(2,'认识侧边栏画面');heading(2,'认识侧边栏画面')
s=sec('screen');svg_diagram(s.xpath('.//svg')[0]);para(rich(s.xpath('.//figcaption')[0]),'small',space=21)
for title,body in [('Live','表示预览正在更新。任务是否完成，还要核对实际结果。'),('点击和拖动','点击画面可点按手机；拖动或触控板双指滑动可滚动、翻页。'),('底部三个按钮','从左到右：刷新连接、回到主屏幕、截图到 Mac 剪贴板。刷新不能解除认证暂停。')]:
    para(title,'h3',space=3);para(body,space=14)
box(rich(s.xpath('./p')[0]))

newpage(3,'怎么下指令');heading(3,'用一句话说明任务')
s=sec('tasks');para(rich(s.xpath('./p')[0]),space=16)
for card in s.xpath('.//div[contains(@class,"card")]'):
    para(tx(card.find('small')),'small',space=3)
    para(tx(card.find('h3')),'h3',space=5)
    box(rich(card.xpath('.//div[@class="prompt"]/p')[0]),style='prompt')
para(rich(s.xpath('./p')[-1]),'small')

newpage(4,'登录与暂停');heading(4,'登录和 Face ID：按图接手')
s=sec('auth')
for e in s.xpath('./p')[:2]: para(rich(e),'body' if e==s.xpath('./p')[0] else 'small',space=13)
steps=s.xpath('.//div[@class="step"]')
for i,e in enumerate(steps):
    global_text=rich(e.find('b'))+'<br/>'+rich(e.find('span'))
    p=Paragraph(global_text,styles['body']);_,ph=p.wrap(WIDTH-65,1000);h=ph+22
    c.setFillColor(HexColor(LIGHT));c.roundRect(M,y-h,WIDTH,h,8,fill=1,stroke=0)
    c.setFillColor(HexColor(GREEN));c.setFont('CJKB',14);c.drawString(M+15,y-24,str(i+1))
    p.drawOn(c,M+45,y-h+11);y-=h+9
    if i<3:
        c.setFillColor(HexColor(GREEN));c.setFont('CJK',13);c.drawCentredString(M+WIDTH/2,y-6,'↓');y-=14
box(rich(s.xpath('./div[contains(@class,"note")]')[0]),style='small')
para(rich(s.xpath('./p')[-1]),'small')
box(rich(s.xpath('./div[@class="prompt"]/p')[0]),style='prompt')

newpage(5,'排错与结束使用');heading(5,'遇到问题，先这样处理')
s=sec('help')
for row in s.xpath('.//tbody/tr'):
    cells=row.findall('td');left=Paragraph(rich(cells[0]),styles['prompt']);right=Paragraph(rich(cells[1]),styles['small'])
    lw=132;rw=WIDTH-lw-26;_,lh=left.wrap(lw,1000);_,rh=right.wrap(rw,1000);h=max(lh,rh)+16
    c.setFillColor(HexColor(LIGHT));c.rect(M,y-h,lw+12,h,fill=1,stroke=0)
    left.drawOn(c,M+7,y-8-lh);right.drawOn(c,M+lw+20,y-8-rh)
    c.setStrokeColor(HexColor(LINE));c.line(M,y-h,M+WIDTH,y-h);y-=h
    if y<55:raise ValueError('Table overflow')
y-=14
box(rich(s.xpath('./div[@class="prompt"]/p')[0]),style='small')
para(rich(s.xpath('./p')[0]),'small',space=15)
heading(6,'核对结果，再停止服务')
s=sec('finish');para(rich(s.xpath('.//div[@class="section-end"]//p')[0]),'small')
box(rich(s.xpath('.//div[@class="prompt"]/p')[0]),style='prompt')
para(rich(s.xpath('.//p[@class="mini"]')[0]),'small',space=10)
para('接线 → 解锁 → 说任务 → 本人认证（如需） → 核对结果 → 停止服务','small',space=0)
newpage(6,'Xcode 可以关闭吗');heading(7,'Xcode 可以关闭，后台服务要保留')
s=sec('xcode');para(rich(s.xpath('./p')[0]),space=14)
svg_diagram(diagrams['xcode']);para(rich(s.xpath('.//figcaption')[0]),'small',space=15)
table_rows(s.xpath('.//tbody/tr'))
box(rich(s.xpath('./p[contains(@class,"note")]')[0]),style='small')
box(rich(s.xpath('./div[@class="prompt"]/p')[0]),style='prompt')

newpage(7,'升级时保留修复');heading(8,'升级前，先保留两项修复')
s=sec('upgrade');para(rich(s.xpath('./p')[0]),space=16)
svg_diagram(diagrams['upgrade'])
for e in s.xpath('.//div[@class="step"]'):
    para('<b>'+rich(e.find('div'))+' · '+rich(e.find('b'))+'</b><br/>'+rich(e.find('span')),'body',space=12)
box(rich(s.xpath('./p[contains(@class,"note")]')[0]),style='small')
para(rich(s.xpath('./p')[2]),'small')
box(rich(s.xpath('./div[@class="prompt"]/p')[0]),style='prompt')
para(rich(s.xpath('./p')[-1]),'small',space=0)

newpage(8,'从备份恢复');heading(9,'有问题时，从备份恢复')
s=sec('restore');para(rich(s.xpath('./p')[0]),space=12)
svg_diagram(diagrams['restore'])
para(tx(s.xpath('./div[@class="panel"]/h3')[0]),'h3',space=8)
for e in s.xpath('./div[@class="panel"]/p'):para(rich(e),'small',space=8)
para(rich(s.xpath('./p')[1]),'small',space=6)
command=escape(tx(s.xpath('./pre/code')[0])).replace('\n','<br/>')
box(command,style='small')
para(rich(s.xpath('./p')[2]),'small',space=8)
box(rich(s.xpath('./div[@class="prompt"]/p')[0]),style='small')
para(rich(s.xpath('./p')[-1]),'small',space=0)
c.save()
published=ROOT/'docs/user-guide'/OUT.name
shutil.copy2(OUT,published)
print(OUT)
