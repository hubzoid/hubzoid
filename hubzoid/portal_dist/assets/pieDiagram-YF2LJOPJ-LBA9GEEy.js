import{$ as e,C as t,E as n,F as r,M as i,N as a,R as o,U as s,b as c,et as l,g as u,rt as d}from"./src-DYX8MXVn.js";import{t as f}from"./mermaid-parser.core-BCNENwJj.js";import{t as p}from"./ordinal-BDEzSJ7C.js";import{n as m}from"./path-fybaL0A-.js";import{p as h}from"./math-Bi_r_ZWc.js";import{t as g}from"./arc-3Xw6uabx.js";import{t as _}from"./array-BifhSqXX.js";import{f as v,r as y}from"./chunk-7DKRZKHE-l6VlBDCM.js";import{t as b}from"./chunk-TMUBEWPD-BsSXlkJX.js";import{o as x}from"./mermaid.core-CJMaAQ5X.js";function S(e,t){return t<e?-1:t>e?1:t>=e?0:NaN}function C(e){return e}function w(){var e=C,t=S,n=null,r=m(0),i=m(h),a=m(0);function o(o){var s,c=(o=_(o)).length,l,u,d=0,f=Array(c),p=Array(c),m=+r.apply(this,arguments),g=Math.min(h,Math.max(-h,i.apply(this,arguments)-m)),v,y=Math.min(Math.abs(g)/c,a.apply(this,arguments)),b=y*(g<0?-1:1),x;for(s=0;s<c;++s)(x=p[f[s]=s]=+e(o[s],s,o))>0&&(d+=x);for(t==null?n!=null&&f.sort(function(e,t){return n(o[e],o[t])}):f.sort(function(e,n){return t(p[e],p[n])}),s=0,u=d?(g-c*b)/d:0;s<c;++s,m=v)l=f[s],x=p[l],v=m+(x>0?x*u:0)+b,p[l]={data:o[l],index:s,value:x,startAngle:m,endAngle:v,padAngle:y};return p}return o.value=function(t){return arguments.length?(e=typeof t==`function`?t:m(+t),o):e},o.sortValues=function(e){return arguments.length?(t=e,n=null,o):t},o.sort=function(e){return arguments.length?(n=e,t=null,o):n},o.startAngle=function(e){return arguments.length?(r=typeof e==`function`?e:m(+e),o):r},o.endAngle=function(e){return arguments.length?(i=typeof e==`function`?e:m(+e),o):i},o.padAngle=function(e){return arguments.length?(a=typeof e==`function`?e:m(+e),o):a},o}var T=n.pie,E={sections:new Map,showData:!1,config:T},D=E.sections,O=E.showData,k=structuredClone(T),A={getConfig:u(()=>structuredClone(k),`getConfig`),clear:u(()=>{D=new Map,O=E.showData,c()},`clear`),setDiagramTitle:d,getDiagramTitle:o,setAccTitle:l,getAccTitle:a,setAccDescription:e,getAccDescription:i,addSection:u(({label:e,value:t})=>{D.has(e)||(D.set(e,t),s.debug(`added new section: ${e}, with value: ${t}`))},`addSection`),getSections:u(()=>D,`getSections`),setShowData:u(e=>{O=e},`setShowData`),getShowData:u(()=>O,`getShowData`)},j=u((e,t)=>{b(e,t),t.setShowData(e.showData),e.sections.map(t.addSection)},`populateDb`),M={parse:u(async e=>{let t=await f(`pie`,e);s.debug(t),j(t,A)},`parse`)},N=u(e=>`
  .pieCircle{
    stroke: ${e.pieStrokeColor};
    stroke-width : ${e.pieStrokeWidth};
    opacity : ${e.pieOpacity};
  }
  .pieOuterCircle{
    stroke: ${e.pieOuterStrokeColor};
    stroke-width: ${e.pieOuterStrokeWidth};
    fill: none;
  }
  .pieTitleText {
    text-anchor: middle;
    font-size: ${e.pieTitleTextSize};
    fill: ${e.pieTitleTextColor};
    font-family: ${e.fontFamily};
  }
  .slice {
    font-family: ${e.fontFamily};
    fill: ${e.pieSectionTextColor};
    font-size:${e.pieSectionTextSize};
    // fill: white;
  }
  .legend text {
    fill: ${e.pieLegendTextColor};
    font-family: ${e.fontFamily};
    font-size: ${e.pieLegendTextSize};
  }
`,`getStyles`),P=u(e=>{let t=[...e.entries()].map(e=>({label:e[0],value:e[1]})).sort((e,t)=>t.value-e.value);return w().value(e=>e.value)(t)},`createPieArcs`),F={parser:M,db:A,renderer:{draw:u((e,n,i,a)=>{s.debug(`rendering pie chart
`+e);let o=a.db,c=r(),l=y(o.getConfig(),c.pie),u=x(n),d=u.append(`g`);d.attr(`transform`,`translate(225,225)`);let{themeVariables:f}=c,[m]=v(f.pieOuterStrokeWidth);m??=2;let h=l.textPosition,_=g().innerRadius(0).outerRadius(185),b=g().innerRadius(185*h).outerRadius(185*h);d.append(`circle`).attr(`cx`,0).attr(`cy`,0).attr(`r`,185+m/2).attr(`class`,`pieOuterCircle`);let S=o.getSections(),C=P(S),w=[f.pie1,f.pie2,f.pie3,f.pie4,f.pie5,f.pie6,f.pie7,f.pie8,f.pie9,f.pie10,f.pie11,f.pie12],T=p(w);d.selectAll(`mySlices`).data(C).enter().append(`path`).attr(`d`,_).attr(`fill`,e=>T(e.data.label)).attr(`class`,`pieCircle`);let E=0;S.forEach(e=>{E+=e}),d.selectAll(`mySlices`).data(C).enter().append(`text`).text(e=>(e.data.value/E*100).toFixed(0)+`%`).attr(`transform`,e=>`translate(`+b.centroid(e)+`)`).style(`text-anchor`,`middle`).attr(`class`,`slice`),d.append(`text`).text(o.getDiagramTitle()).attr(`x`,0).attr(`y`,-200).attr(`class`,`pieTitleText`);let D=d.selectAll(`.legend`).data(T.domain()).enter().append(`g`).attr(`class`,`legend`).attr(`transform`,(e,t)=>{let n=22*T.domain().length/2;return`translate(216,`+(t*22-n)+`)`});D.append(`rect`).attr(`width`,18).attr(`height`,18).style(`fill`,T).style(`stroke`,T),D.data(C).append(`text`).attr(`x`,22).attr(`y`,14).text(e=>{let{label:t,value:n}=e.data;return o.getShowData()?`${t} [${n}]`:t});let O=512+Math.max(...D.selectAll(`text`).nodes().map(e=>e?.getBoundingClientRect().width??0));u.attr(`viewBox`,`0 0 ${O} 450`),t(u,450,O,l.useMaxWidth)},`draw`)},styles:N};export{F as diagram};