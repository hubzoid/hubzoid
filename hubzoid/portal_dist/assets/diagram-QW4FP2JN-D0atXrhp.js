import{t as e}from"./mermaid-parser.core-CPRrhHDS.js";import{$ as t,C as n,E as r,M as i,N as a,P as o,R as s,U as c,b as l,et as u,g as d,rt as f}from"./src-DUoXgEln.js";import{r as p}from"./chunk-7DKRZKHE-BOaUIwOY.js";import{t as m}from"./chunk-TMUBEWPD-Cxe8MPvD.js";import{o as h}from"./mermaid.core-DlOEhkVa.js";var g={packet:[]},_=structuredClone(g),v=r.packet,y={pushWord:d(e=>{e.length>0&&_.packet.push(e)},`pushWord`),getPacket:d(()=>_.packet,`getPacket`),getConfig:d(()=>{let e=p({...v,...o().packet});return e.showBits&&(e.paddingY+=10),e},`getConfig`),clear:d(()=>{l(),_=structuredClone(g)},`clear`),setAccTitle:u,getAccTitle:a,setDiagramTitle:f,getDiagramTitle:s,getAccDescription:i,setAccDescription:t},b=1e4,x=d(e=>{m(e,y);let t=-1,n=[],r=1,{bitsPerRow:i}=y.getConfig();for(let{start:a,end:o,label:s}of e.blocks){if(o&&o<a)throw Error(`Packet block ${a} - ${o} is invalid. End must be greater than start.`);if(a!==t+1)throw Error(`Packet block ${a} - ${o??a} is not contiguous. It should start from ${t+1}.`);for(t=o??a,c.debug(`Packet block ${a} - ${t} with label ${s}`);n.length<=i+1&&y.getPacket().length<b;){let[e,t]=S({start:a,end:o,label:s},r,i);if(n.push(e),e.end+1===r*i&&(y.pushWord(n),n=[],r++),!t)break;({start:a,end:o,label:s}=t)}}y.pushWord(n)},`populate`),S=d((e,t,n)=>{if(e.end===void 0&&(e.end=e.start),e.start>e.end)throw Error(`Block start ${e.start} is greater than block end ${e.end}.`);return e.end+1<=t*n?[e,void 0]:[{start:e.start,end:t*n-1,label:e.label},{start:t*n,end:e.end,label:e.label}]},`getNextFittingBlock`),C={parse:d(async t=>{let n=await e(`packet`,t);c.debug(n),x(n)},`parse`)},w=d((e,t,r,i)=>{let a=i.db,o=a.getConfig(),{rowHeight:s,paddingY:c,bitWidth:l,bitsPerRow:u}=o,d=a.getPacket(),f=a.getDiagramTitle(),p=s+c,m=p*(d.length+1)-(f?0:s),g=l*u+2,_=h(t);_.attr(`viewbox`,`0 0 ${g} ${m}`),n(_,m,g,o.useMaxWidth);for(let[e,t]of d.entries())T(_,t,e,o);_.append(`text`).text(f).attr(`x`,g/2).attr(`y`,m-p/2).attr(`dominant-baseline`,`middle`).attr(`text-anchor`,`middle`).attr(`class`,`packetTitle`)},`draw`),T=d((e,t,n,{rowHeight:r,paddingX:i,paddingY:a,bitWidth:o,bitsPerRow:s,showBits:c})=>{let l=e.append(`g`),u=n*(r+a)+a;for(let e of t){let t=e.start%s*o+1,n=(e.end-e.start+1)*o-i;if(l.append(`rect`).attr(`x`,t).attr(`y`,u).attr(`width`,n).attr(`height`,r).attr(`class`,`packetBlock`),l.append(`text`).attr(`x`,t+n/2).attr(`y`,u+r/2).attr(`class`,`packetLabel`).attr(`dominant-baseline`,`middle`).attr(`text-anchor`,`middle`).text(e.label),!c)continue;let a=e.end===e.start,d=u-2;l.append(`text`).attr(`x`,t+(a?n/2:0)).attr(`y`,d).attr(`class`,`packetByte start`).attr(`dominant-baseline`,`auto`).attr(`text-anchor`,a?`middle`:`start`).text(e.start),a||l.append(`text`).attr(`x`,t+n).attr(`y`,d).attr(`class`,`packetByte end`).attr(`dominant-baseline`,`auto`).attr(`text-anchor`,`end`).text(e.end)}},`drawWord`),E={draw:w},D={byteFontSize:`10px`,startByteColor:`black`,endByteColor:`black`,labelColor:`black`,labelFontSize:`12px`,titleColor:`black`,titleFontSize:`14px`,blockStrokeColor:`black`,blockStrokeWidth:`1`,blockFillColor:`#efefef`},O={parser:C,db:y,renderer:E,styles:d(({packet:e}={})=>{let t=p(D,e);return`
	.packetByte {
		font-size: ${t.byteFontSize};
	}
	.packetByte.start {
		fill: ${t.startByteColor};
	}
	.packetByte.end {
		fill: ${t.endByteColor};
	}
	.packetLabel {
		fill: ${t.labelColor};
		font-size: ${t.labelFontSize};
	}
	.packetTitle {
		fill: ${t.titleColor};
		font-size: ${t.titleFontSize};
	}
	.packetBlock {
		stroke: ${t.blockStrokeColor};
		stroke-width: ${t.blockStrokeWidth};
		fill: ${t.blockFillColor};
	}
	`},`styles`)};export{O as diagram};