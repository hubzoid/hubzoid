import{$ as e,F as t,M as n,N as r,R as i,S as a,U as o,b as s,et as c,g as l,rt as u}from"./src-NoJ7zJkN.js";import{h as d,o as f}from"./chunk-7DKRZKHE-uKzm_muA.js";import{n as p,t as m}from"./chunk-5HRBRIJM-DmSGteoW.js";import{a as h}from"./mermaid.core-TMnrrfOg.js";var g=function(){var e=l(function(e,t,n,r){for(n||={},r=e.length;r--;n[e[r]]=t);return n},`o`),t=[1,2],n=[1,3],r=[1,4],i=[2,4],a=[1,9],o=[1,11],s=[1,16],c=[1,17],u=[1,18],d=[1,19],f=[1,32],p=[1,20],m=[1,21],h=[1,22],g=[1,23],_=[1,24],v=[1,26],y=[1,27],b=[1,28],x=[1,29],S=[1,30],C=[1,31],w=[1,34],T=[1,35],E=[1,36],D=[1,37],O=[1,33],k=[1,4,5,16,17,19,21,22,24,25,26,27,28,29,33,35,37,38,42,45,48,49,50,51,54],A=[1,4,5,14,15,16,17,19,21,22,24,25,26,27,28,29,33,35,37,38,42,45,48,49,50,51,54],j=[4,5,16,17,19,21,22,24,25,26,27,28,29,33,35,37,38,42,45,48,49,50,51,54],M={trace:l(function(){},`trace`),yy:{},symbols_:{error:2,start:3,SPACE:4,NL:5,SD:6,document:7,line:8,statement:9,classDefStatement:10,styleStatement:11,cssClassStatement:12,idStatement:13,DESCR:14,"-->":15,HIDE_EMPTY:16,scale:17,WIDTH:18,COMPOSIT_STATE:19,STRUCT_START:20,STRUCT_STOP:21,STATE_DESCR:22,AS:23,ID:24,FORK:25,JOIN:26,CHOICE:27,CONCURRENT:28,note:29,notePosition:30,NOTE_TEXT:31,direction:32,acc_title:33,acc_title_value:34,acc_descr:35,acc_descr_value:36,acc_descr_multiline_value:37,classDef:38,CLASSDEF_ID:39,CLASSDEF_STYLEOPTS:40,DEFAULT:41,style:42,STYLE_IDS:43,STYLEDEF_STYLEOPTS:44,class:45,CLASSENTITY_IDS:46,STYLECLASS:47,direction_tb:48,direction_bt:49,direction_rl:50,direction_lr:51,eol:52,";":53,EDGE_STATE:54,STYLE_SEPARATOR:55,left_of:56,right_of:57,$accept:0,$end:1},terminals_:{2:`error`,4:`SPACE`,5:`NL`,6:`SD`,14:`DESCR`,15:`-->`,16:`HIDE_EMPTY`,17:`scale`,18:`WIDTH`,19:`COMPOSIT_STATE`,20:`STRUCT_START`,21:`STRUCT_STOP`,22:`STATE_DESCR`,23:`AS`,24:`ID`,25:`FORK`,26:`JOIN`,27:`CHOICE`,28:`CONCURRENT`,29:`note`,31:`NOTE_TEXT`,33:`acc_title`,34:`acc_title_value`,35:`acc_descr`,36:`acc_descr_value`,37:`acc_descr_multiline_value`,38:`classDef`,39:`CLASSDEF_ID`,40:`CLASSDEF_STYLEOPTS`,41:`DEFAULT`,42:`style`,43:`STYLE_IDS`,44:`STYLEDEF_STYLEOPTS`,45:`class`,46:`CLASSENTITY_IDS`,47:`STYLECLASS`,48:`direction_tb`,49:`direction_bt`,50:`direction_rl`,51:`direction_lr`,53:`;`,54:`EDGE_STATE`,55:`STYLE_SEPARATOR`,56:`left_of`,57:`right_of`},productions_:[0,[3,2],[3,2],[3,2],[7,0],[7,2],[8,2],[8,1],[8,1],[9,1],[9,1],[9,1],[9,1],[9,2],[9,3],[9,4],[9,1],[9,2],[9,1],[9,4],[9,3],[9,6],[9,1],[9,1],[9,1],[9,1],[9,4],[9,4],[9,1],[9,2],[9,2],[9,1],[10,3],[10,3],[11,3],[12,3],[32,1],[32,1],[32,1],[32,1],[52,1],[52,1],[13,1],[13,1],[13,3],[13,3],[30,1],[30,1]],performAction:l(function(e,t,n,r,i,a,o){var s=a.length-1;switch(i){case 3:return r.setRootDoc(a[s]),a[s];case 4:this.$=[];break;case 5:a[s]!=`nl`&&(a[s-1].push(a[s]),this.$=a[s-1]);break;case 6:case 7:this.$=a[s];break;case 8:this.$=`nl`;break;case 12:this.$=a[s];break;case 13:let e=a[s-1];e.description=r.trimColon(a[s]),this.$=e;break;case 14:this.$={stmt:`relation`,state1:a[s-2],state2:a[s]};break;case 15:let t=r.trimColon(a[s]);this.$={stmt:`relation`,state1:a[s-3],state2:a[s-1],description:t};break;case 19:this.$={stmt:`state`,id:a[s-3],type:`default`,description:``,doc:a[s-1]};break;case 20:var c=a[s],l=a[s-2].trim();if(a[s].match(`:`)){var u=a[s].split(`:`);c=u[0],l=[l,u[1]]}this.$={stmt:`state`,id:c,type:`default`,description:l};break;case 21:this.$={stmt:`state`,id:a[s-3],type:`default`,description:a[s-5],doc:a[s-1]};break;case 22:this.$={stmt:`state`,id:a[s],type:`fork`};break;case 23:this.$={stmt:`state`,id:a[s],type:`join`};break;case 24:this.$={stmt:`state`,id:a[s],type:`choice`};break;case 25:this.$={stmt:`state`,id:r.getDividerId(),type:`divider`};break;case 26:this.$={stmt:`state`,id:a[s-1].trim(),note:{position:a[s-2].trim(),text:a[s].trim()}};break;case 29:this.$=a[s].trim(),r.setAccTitle(this.$);break;case 30:case 31:this.$=a[s].trim(),r.setAccDescription(this.$);break;case 32:case 33:this.$={stmt:`classDef`,id:a[s-1].trim(),classes:a[s].trim()};break;case 34:this.$={stmt:`style`,id:a[s-1].trim(),styleClass:a[s].trim()};break;case 35:this.$={stmt:`applyClass`,id:a[s-1].trim(),styleClass:a[s].trim()};break;case 36:r.setDirection(`TB`),this.$={stmt:`dir`,value:`TB`};break;case 37:r.setDirection(`BT`),this.$={stmt:`dir`,value:`BT`};break;case 38:r.setDirection(`RL`),this.$={stmt:`dir`,value:`RL`};break;case 39:r.setDirection(`LR`),this.$={stmt:`dir`,value:`LR`};break;case 42:case 43:this.$={stmt:`state`,id:a[s].trim(),type:`default`,description:``};break;case 44:this.$={stmt:`state`,id:a[s-2].trim(),classes:[a[s].trim()],type:`default`,description:``};break;case 45:this.$={stmt:`state`,id:a[s-2].trim(),classes:[a[s].trim()],type:`default`,description:``}}},`anonymous`),table:[{3:1,4:t,5:n,6:r},{1:[3]},{3:5,4:t,5:n,6:r},{3:6,4:t,5:n,6:r},e([1,4,5,16,17,19,22,24,25,26,27,28,29,33,35,37,38,42,45,48,49,50,51,54],i,{7:7}),{1:[2,1]},{1:[2,2]},{1:[2,3],4:a,5:o,8:8,9:10,10:12,11:13,12:14,13:15,16:s,17:c,19:u,22:d,24:f,25:p,26:m,27:h,28:g,29:_,32:25,33:v,35:y,37:b,38:x,42:S,45:C,48:w,49:T,50:E,51:D,54:O},e(k,[2,5]),{9:38,10:12,11:13,12:14,13:15,16:s,17:c,19:u,22:d,24:f,25:p,26:m,27:h,28:g,29:_,32:25,33:v,35:y,37:b,38:x,42:S,45:C,48:w,49:T,50:E,51:D,54:O},e(k,[2,7]),e(k,[2,8]),e(k,[2,9]),e(k,[2,10]),e(k,[2,11]),e(k,[2,12],{14:[1,39],15:[1,40]}),e(k,[2,16]),{18:[1,41]},e(k,[2,18],{20:[1,42]}),{23:[1,43]},e(k,[2,22]),e(k,[2,23]),e(k,[2,24]),e(k,[2,25]),{30:44,31:[1,45],56:[1,46],57:[1,47]},e(k,[2,28]),{34:[1,48]},{36:[1,49]},e(k,[2,31]),{39:[1,50],41:[1,51]},{43:[1,52]},{46:[1,53]},e(A,[2,42],{55:[1,54]}),e(A,[2,43],{55:[1,55]}),e(k,[2,36]),e(k,[2,37]),e(k,[2,38]),e(k,[2,39]),e(k,[2,6]),e(k,[2,13]),{13:56,24:f,54:O},e(k,[2,17]),e(j,i,{7:57}),{24:[1,58]},{24:[1,59]},{23:[1,60]},{24:[2,46]},{24:[2,47]},e(k,[2,29]),e(k,[2,30]),{40:[1,61]},{40:[1,62]},{44:[1,63]},{47:[1,64]},{24:[1,65]},{24:[1,66]},e(k,[2,14],{14:[1,67]}),{4:a,5:o,8:8,9:10,10:12,11:13,12:14,13:15,16:s,17:c,19:u,21:[1,68],22:d,24:f,25:p,26:m,27:h,28:g,29:_,32:25,33:v,35:y,37:b,38:x,42:S,45:C,48:w,49:T,50:E,51:D,54:O},e(k,[2,20],{20:[1,69]}),{31:[1,70]},{24:[1,71]},e(k,[2,32]),e(k,[2,33]),e(k,[2,34]),e(k,[2,35]),e(A,[2,44]),e(A,[2,45]),e(k,[2,15]),e(k,[2,19]),e(j,i,{7:72}),e(k,[2,26]),e(k,[2,27]),{4:a,5:o,8:8,9:10,10:12,11:13,12:14,13:15,16:s,17:c,19:u,21:[1,73],22:d,24:f,25:p,26:m,27:h,28:g,29:_,32:25,33:v,35:y,37:b,38:x,42:S,45:C,48:w,49:T,50:E,51:D,54:O},e(k,[2,21])],defaultActions:{5:[2,1],6:[2,2],46:[2,46],47:[2,47]},parseError:l(function(e,t){if(t.recoverable)this.trace(e);else{var n=Error(e);throw n.hash=t,n}},`parseError`),parse:l(function(e){var t=this,n=[0],r=[],i=[null],a=[],o=this.table,s=``,c=0,u=0,d=0,f=2,p=1,m=a.slice.call(arguments,1),h=Object.create(this.lexer),g={yy:{}};for(var _ in this.yy)Object.prototype.hasOwnProperty.call(this.yy,_)&&(g.yy[_]=this.yy[_]);h.setInput(e,g.yy),g.yy.lexer=h,g.yy.parser=this,h.yylloc===void 0&&(h.yylloc={});var v=h.yylloc;a.push(v);var y=h.options&&h.options.ranges;this.parseError=typeof g.yy.parseError==`function`?g.yy.parseError:Object.getPrototypeOf(this).parseError;function b(e){n.length-=2*e,i.length-=e,a.length-=e}l(b,`popStack`);function x(){var e=r.pop()||h.lex()||p;return typeof e!=`number`&&(e instanceof Array&&(r=e,e=r.pop()),e=t.symbols_[e]||e),e}l(x,`lex`);for(var S,C,w,T,E,D={},O,k,A,j;;){if(w=n[n.length-1],this.defaultActions[w]?T=this.defaultActions[w]:(S??=x(),T=o[w]&&o[w][S]),T===void 0||!T.length||!T[0]){var M=``;for(O in j=[],o[w])this.terminals_[O]&&O>f&&j.push(`'`+this.terminals_[O]+`'`);M=h.showPosition?`Parse error on line `+(c+1)+`:
`+h.showPosition()+`
Expecting `+j.join(`, `)+`, got '`+(this.terminals_[S]||S)+`'`:`Parse error on line `+(c+1)+`: Unexpected `+(S==p?`end of input`:`'`+(this.terminals_[S]||S)+`'`),this.parseError(M,{text:h.match,token:this.terminals_[S]||S,line:h.yylineno,loc:v,expected:j})}if(T[0]instanceof Array&&T.length>1)throw Error(`Parse Error: multiple actions possible at state: `+w+`, token: `+S);switch(T[0]){case 1:n.push(S),i.push(h.yytext),a.push(h.yylloc),n.push(T[1]),S=null,C?(S=C,C=null):(u=h.yyleng,s=h.yytext,c=h.yylineno,v=h.yylloc,d>0&&d--);break;case 2:if(k=this.productions_[T[1]][1],D.$=i[i.length-k],D._$={first_line:a[a.length-(k||1)].first_line,last_line:a[a.length-1].last_line,first_column:a[a.length-(k||1)].first_column,last_column:a[a.length-1].last_column},y&&(D._$.range=[a[a.length-(k||1)].range[0],a[a.length-1].range[1]]),E=this.performAction.apply(D,[s,u,c,g.yy,T[1],i,a].concat(m)),E!==void 0)return E;k&&(n=n.slice(0,-1*k*2),i=i.slice(0,-1*k),a=a.slice(0,-1*k)),n.push(this.productions_[T[1]][0]),i.push(D.$),a.push(D._$),A=o[n[n.length-2]][n[n.length-1]],n.push(A);break;case 3:return!0}}return!0},`parse`)};M.lexer=function(){return{EOF:1,parseError:l(function(e,t){if(this.yy.parser)this.yy.parser.parseError(e,t);else throw Error(e)},`parseError`),setInput:l(function(e,t){return this.yy=t||this.yy||{},this._input=e,this._more=this._backtrack=this.done=!1,this.yylineno=this.yyleng=0,this.yytext=this.matched=this.match=``,this.conditionStack=[`INITIAL`],this.yylloc={first_line:1,first_column:0,last_line:1,last_column:0},this.options.ranges&&(this.yylloc.range=[0,0]),this.offset=0,this},`setInput`),input:l(function(){var e=this._input[0];return this.yytext+=e,this.yyleng++,this.offset++,this.match+=e,this.matched+=e,e.match(/(?:\r\n?|\n).*/g)?(this.yylineno++,this.yylloc.last_line++):this.yylloc.last_column++,this.options.ranges&&this.yylloc.range[1]++,this._input=this._input.slice(1),e},`input`),unput:l(function(e){var t=e.length,n=e.split(/(?:\r\n?|\n)/g);this._input=e+this._input,this.yytext=this.yytext.substr(0,this.yytext.length-t),this.offset-=t;var r=this.match.split(/(?:\r\n?|\n)/g);this.match=this.match.substr(0,this.match.length-1),this.matched=this.matched.substr(0,this.matched.length-1),n.length-1&&(this.yylineno-=n.length-1);var i=this.yylloc.range;return this.yylloc={first_line:this.yylloc.first_line,last_line:this.yylineno+1,first_column:this.yylloc.first_column,last_column:n?(n.length===r.length?this.yylloc.first_column:0)+r[r.length-n.length].length-n[0].length:this.yylloc.first_column-t},this.options.ranges&&(this.yylloc.range=[i[0],i[0]+this.yyleng-t]),this.yyleng=this.yytext.length,this},`unput`),more:l(function(){return this._more=!0,this},`more`),reject:l(function(){if(this.options.backtrack_lexer)this._backtrack=!0;else return this.parseError(`Lexical error on line `+(this.yylineno+1)+`. You can only invoke reject() in the lexer when the lexer is of the backtracking persuasion (options.backtrack_lexer = true).
`+this.showPosition(),{text:``,token:null,line:this.yylineno});return this},`reject`),less:l(function(e){this.unput(this.match.slice(e))},`less`),pastInput:l(function(){var e=this.matched.substr(0,this.matched.length-this.match.length);return(e.length>20?`...`:``)+e.substr(-20).replace(/\n/g,``)},`pastInput`),upcomingInput:l(function(){var e=this.match;return e.length<20&&(e+=this._input.substr(0,20-e.length)),(e.substr(0,20)+(e.length>20?`...`:``)).replace(/\n/g,``)},`upcomingInput`),showPosition:l(function(){var e=this.pastInput(),t=Array(e.length+1).join(`-`);return e+this.upcomingInput()+`
`+t+`^`},`showPosition`),test_match:l(function(e,t){var n,r,i;if(this.options.backtrack_lexer&&(i={yylineno:this.yylineno,yylloc:{first_line:this.yylloc.first_line,last_line:this.last_line,first_column:this.yylloc.first_column,last_column:this.yylloc.last_column},yytext:this.yytext,match:this.match,matches:this.matches,matched:this.matched,yyleng:this.yyleng,offset:this.offset,_more:this._more,_input:this._input,yy:this.yy,conditionStack:this.conditionStack.slice(0),done:this.done},this.options.ranges&&(i.yylloc.range=this.yylloc.range.slice(0))),r=e[0].match(/(?:\r\n?|\n).*/g),r&&(this.yylineno+=r.length),this.yylloc={first_line:this.yylloc.last_line,last_line:this.yylineno+1,first_column:this.yylloc.last_column,last_column:r?r[r.length-1].length-r[r.length-1].match(/\r?\n?/)[0].length:this.yylloc.last_column+e[0].length},this.yytext+=e[0],this.match+=e[0],this.matches=e,this.yyleng=this.yytext.length,this.options.ranges&&(this.yylloc.range=[this.offset,this.offset+=this.yyleng]),this._more=!1,this._backtrack=!1,this._input=this._input.slice(e[0].length),this.matched+=e[0],n=this.performAction.call(this,this.yy,this,t,this.conditionStack[this.conditionStack.length-1]),this.done&&this._input&&(this.done=!1),n)return n;if(this._backtrack){for(var a in i)this[a]=i[a];return!1}return!1},`test_match`),next:l(function(){if(this.done)return this.EOF;this._input||(this.done=!0);var e,t,n,r;this._more||(this.yytext=``,this.match=``);for(var i=this._currentRules(),a=0;a<i.length;a++)if(n=this._input.match(this.rules[i[a]]),n&&(!t||n[0].length>t[0].length)){if(t=n,r=a,this.options.backtrack_lexer){if(e=this.test_match(n,i[a]),e!==!1)return e;if(this._backtrack){t=!1;continue}return!1}if(!this.options.flex)break}return t?(e=this.test_match(t,i[r]),e!==!1&&e):this._input===``?this.EOF:this.parseError(`Lexical error on line `+(this.yylineno+1)+`. Unrecognized text.
`+this.showPosition(),{text:``,token:null,line:this.yylineno})},`next`),lex:l(function(){return this.next()||this.lex()},`lex`),begin:l(function(e){this.conditionStack.push(e)},`begin`),popState:l(function(){return this.conditionStack.length-1>0?this.conditionStack.pop():this.conditionStack[0]},`popState`),_currentRules:l(function(){return this.conditionStack.length&&this.conditionStack[this.conditionStack.length-1]?this.conditions[this.conditionStack[this.conditionStack.length-1]].rules:this.conditions.INITIAL.rules},`_currentRules`),topState:l(function(e){return e=this.conditionStack.length-1-Math.abs(e||0),e>=0?this.conditionStack[e]:`INITIAL`},`topState`),pushState:l(function(e){this.begin(e)},`pushState`),stateStackSize:l(function(){return this.conditionStack.length},`stateStackSize`),options:{"case-insensitive":!0},performAction:l(function(e,t,n,r){switch(n){case 0:return 41;case 1:return 48;case 2:return 49;case 3:return 50;case 4:return 51;case 5:break;case 6:break;case 7:return 5;case 8:break;case 9:break;case 10:break;case 11:break;case 12:return this.pushState(`SCALE`),17;case 13:return 18;case 14:this.popState();break;case 15:return this.begin(`acc_title`),33;case 16:return this.popState(),`acc_title_value`;case 17:return this.begin(`acc_descr`),35;case 18:return this.popState(),`acc_descr_value`;case 19:this.begin(`acc_descr_multiline`);break;case 20:this.popState();break;case 21:return`acc_descr_multiline_value`;case 22:return this.pushState(`CLASSDEF`),38;case 23:return this.popState(),this.pushState(`CLASSDEFID`),`DEFAULT_CLASSDEF_ID`;case 24:return this.popState(),this.pushState(`CLASSDEFID`),39;case 25:return this.popState(),40;case 26:return this.pushState(`CLASS`),45;case 27:return this.popState(),this.pushState(`CLASS_STYLE`),46;case 28:return this.popState(),47;case 29:return this.pushState(`STYLE`),42;case 30:return this.popState(),this.pushState(`STYLEDEF_STYLES`),43;case 31:return this.popState(),44;case 32:return this.pushState(`SCALE`),17;case 33:return 18;case 34:this.popState();break;case 35:this.pushState(`STATE`);break;case 36:return this.popState(),t.yytext=t.yytext.slice(0,-8).trim(),25;case 37:return this.popState(),t.yytext=t.yytext.slice(0,-8).trim(),26;case 38:return this.popState(),t.yytext=t.yytext.slice(0,-10).trim(),27;case 39:return this.popState(),t.yytext=t.yytext.slice(0,-8).trim(),25;case 40:return this.popState(),t.yytext=t.yytext.slice(0,-8).trim(),26;case 41:return this.popState(),t.yytext=t.yytext.slice(0,-10).trim(),27;case 42:return 48;case 43:return 49;case 44:return 50;case 45:return 51;case 46:this.pushState(`STATE_STRING`);break;case 47:return this.pushState(`STATE_ID`),`AS`;case 48:return this.popState(),`ID`;case 49:this.popState();break;case 50:return`STATE_DESCR`;case 51:return 19;case 52:this.popState();break;case 53:return this.popState(),this.pushState(`struct`),20;case 54:break;case 55:return this.popState(),21;case 56:break;case 57:return this.begin(`NOTE`),29;case 58:return this.popState(),this.pushState(`NOTE_ID`),56;case 59:return this.popState(),this.pushState(`NOTE_ID`),57;case 60:this.popState(),this.pushState(`FLOATING_NOTE`);break;case 61:return this.popState(),this.pushState(`FLOATING_NOTE_ID`),`AS`;case 62:break;case 63:return`NOTE_TEXT`;case 64:return this.popState(),`ID`;case 65:return this.popState(),this.pushState(`NOTE_TEXT`),24;case 66:return this.popState(),t.yytext=t.yytext.substr(2).trim(),31;case 67:return this.popState(),t.yytext=t.yytext.slice(0,-8).trim(),31;case 68:return 6;case 69:return 6;case 70:return 16;case 71:return 54;case 72:return 24;case 73:return t.yytext=t.yytext.trim(),14;case 74:return 15;case 75:return 28;case 76:return 55;case 77:return 5;case 78:return`INVALID`}},`anonymous`),rules:[/^(?:default\b)/i,/^(?:.*direction\s+TB[^\n]*)/i,/^(?:.*direction\s+BT[^\n]*)/i,/^(?:.*direction\s+RL[^\n]*)/i,/^(?:.*direction\s+LR[^\n]*)/i,/^(?:%%(?!\{)[^\n]*)/i,/^(?:[^\}]%%[^\n]*)/i,/^(?:[\n]+)/i,/^(?:[\s]+)/i,/^(?:((?!\n)\s)+)/i,/^(?:#[^\n]*)/i,/^(?:%[^\n]*)/i,/^(?:scale\s+)/i,/^(?:\d+)/i,/^(?:\s+width\b)/i,/^(?:accTitle\s*:\s*)/i,/^(?:(?!\n||)*[^\n]*)/i,/^(?:accDescr\s*:\s*)/i,/^(?:(?!\n||)*[^\n]*)/i,/^(?:accDescr\s*\{\s*)/i,/^(?:[\}])/i,/^(?:[^\}]*)/i,/^(?:classDef\s+)/i,/^(?:DEFAULT\s+)/i,/^(?:\w+\s+)/i,/^(?:[^\n]*)/i,/^(?:class\s+)/i,/^(?:(\w+)+((,\s*\w+)*))/i,/^(?:[^\n]*)/i,/^(?:style\s+)/i,/^(?:[\w,]+\s+)/i,/^(?:[^\n]*)/i,/^(?:scale\s+)/i,/^(?:\d+)/i,/^(?:\s+width\b)/i,/^(?:state\s+)/i,/^(?:.*<<fork>>)/i,/^(?:.*<<join>>)/i,/^(?:.*<<choice>>)/i,/^(?:.*\[\[fork\]\])/i,/^(?:.*\[\[join\]\])/i,/^(?:.*\[\[choice\]\])/i,/^(?:.*direction\s+TB[^\n]*)/i,/^(?:.*direction\s+BT[^\n]*)/i,/^(?:.*direction\s+RL[^\n]*)/i,/^(?:.*direction\s+LR[^\n]*)/i,/^(?:["])/i,/^(?:\s*as\s+)/i,/^(?:[^\n\{]*)/i,/^(?:["])/i,/^(?:[^"]*)/i,/^(?:[^\n\s\{]+)/i,/^(?:\n)/i,/^(?:\{)/i,/^(?:%%(?!\{)[^\n]*)/i,/^(?:\})/i,/^(?:[\n])/i,/^(?:note\s+)/i,/^(?:left of\b)/i,/^(?:right of\b)/i,/^(?:")/i,/^(?:\s*as\s*)/i,/^(?:["])/i,/^(?:[^"]*)/i,/^(?:[^\n]*)/i,/^(?:\s*[^:\n\s\-]+)/i,/^(?:\s*:[^:\n;]+)/i,/^(?:[\s\S]*?end note\b)/i,/^(?:stateDiagram\s+)/i,/^(?:stateDiagram-v2\s+)/i,/^(?:hide empty description\b)/i,/^(?:\[\*\])/i,/^(?:[^:\n\s\-\{]+)/i,/^(?:\s*:[^:\n;]+)/i,/^(?:-->)/i,/^(?:--)/i,/^(?::::)/i,/^(?:$)/i,/^(?:.)/i],conditions:{LINE:{rules:[9,10],inclusive:!1},struct:{rules:[9,10,22,26,29,35,42,43,44,45,54,55,56,57,71,72,73,74,75],inclusive:!1},FLOATING_NOTE_ID:{rules:[64],inclusive:!1},FLOATING_NOTE:{rules:[61,62,63],inclusive:!1},NOTE_TEXT:{rules:[66,67],inclusive:!1},NOTE_ID:{rules:[65],inclusive:!1},NOTE:{rules:[58,59,60],inclusive:!1},STYLEDEF_STYLEOPTS:{rules:[],inclusive:!1},STYLEDEF_STYLES:{rules:[31],inclusive:!1},STYLE_IDS:{rules:[],inclusive:!1},STYLE:{rules:[30],inclusive:!1},CLASS_STYLE:{rules:[28],inclusive:!1},CLASS:{rules:[27],inclusive:!1},CLASSDEFID:{rules:[25],inclusive:!1},CLASSDEF:{rules:[23,24],inclusive:!1},acc_descr_multiline:{rules:[20,21],inclusive:!1},acc_descr:{rules:[18],inclusive:!1},acc_title:{rules:[16],inclusive:!1},SCALE:{rules:[13,14,33,34],inclusive:!1},ALIAS:{rules:[],inclusive:!1},STATE_ID:{rules:[48],inclusive:!1},STATE_STRING:{rules:[49,50],inclusive:!1},FORK_STATE:{rules:[],inclusive:!1},STATE:{rules:[9,10,36,37,38,39,40,41,46,47,51,52,53],inclusive:!1},ID:{rules:[9,10],inclusive:!1},INITIAL:{rules:[0,1,2,3,4,5,6,7,8,10,11,12,15,17,19,22,26,29,32,35,53,57,68,69,70,71,72,73,74,76,77,78],inclusive:!0}}}}();function N(){this.yy={}}return l(N,`Parser`),N.prototype=M,M.Parser=N,new N}();g.parser=g;var _=g,v=`LR`,y=`TB`,b=`state`,x=`relation`,S=`classDef`,C=`style`,w=`applyClass`,T=`default`,E=`divider`,D=`fill:none`,O=`fill: #333`,k=`c`,A=`text`,j=`normal`,M=`rect`,N=`rectWithTitle`,ee=`stateStart`,te=`stateEnd`,ne=`divider`,re=`roundedWithTitle`,ie=`note`,ae=`noteGroup`,P=`statediagram`,oe=`${P}-state`,se=`transition`,ce=`note`,le=`${se} note-edge`,ue=`${P}-${ce}`,de=`${P}-cluster`,fe=`${P}-cluster-alt`,pe=`parent`,me=`note`,he=`state`,ge=`----`,_e=`${ge}${me}`,ve=`${ge}${pe}`,ye=l((e,t=y)=>{if(!e.doc)return t;let n=t;for(let t of e.doc)t.stmt===`dir`&&(n=t.value);return n},`getDir`),be={getClasses:l(function(e,t){return t.db.extract(t.db.getRootDocV2()),t.db.getClasses()},`getClasses`),draw:l(async function(e,n,r,i){o.info(`REF0:`),o.info(`Drawing state diagram (v2)`,n);let{securityLevel:a,state:s,layout:c}=t();i.db.extract(i.db.getRootDocV2());let l=i.db.getData(),u=m(n,a);l.type=i.type,l.layoutAlgorithm=c,l.nodeSpacing=s?.nodeSpacing||50,l.rankSpacing=s?.rankSpacing||50,l.markers=[`barb`],l.diagramId=n,await h(l,u),d.insertTitle(u,`statediagramTitleText`,s?.titleTopMargin??25,i.db.getDiagramTitle()),p(u,8,P,s?.useMaxWidth??!0)},`draw`),getDir:ye},F=new Map,I=0;function L(e=``,t=0,n=``,r=ge){return`${he}-${e}${n!==null&&n.length>0?`${r}${n}`:``}-${t}`}l(L,`stateDomId`);var xe=l((e,n,r,i,s,c,l,u)=>{o.trace(`items`,n),n.forEach(n=>{switch(n.stmt){case b:z(e,n,r,i,s,c,l,u);break;case T:z(e,n,r,i,s,c,l,u);break;case x:{z(e,n.state1,r,i,s,c,l,u),z(e,n.state2,r,i,s,c,l,u);let o={id:`edge`+I,start:n.state1.id,end:n.state2.id,arrowhead:`normal`,arrowTypeEnd:`arrow_barb`,style:D,labelStyle:``,label:a.sanitizeText(n.description,t()),arrowheadStyle:O,labelpos:k,labelType:A,thickness:j,classes:se,look:l};s.push(o),I++}}})},`setupDoc`),Se=l((e,t=y)=>{let n=t;if(e.doc)for(let t of e.doc)t.stmt===`dir`&&(n=t.value);return n},`getDir`);function R(e,t,n){if(!t.id||t.id===`</join></fork>`||t.id===`</choice>`)return;t.cssClasses&&(Array.isArray(t.cssCompiledStyles)||(t.cssCompiledStyles=[]),t.cssClasses.split(` `).forEach(e=>{if(n.get(e)){let r=n.get(e);t.cssCompiledStyles=[...t.cssCompiledStyles,...r.styles]}}));let r=e.find(e=>e.id===t.id);r?Object.assign(r,t):e.push(t)}l(R,`insertOrUpdateNode`);function Ce(e){return e?.classes?.join(` `)??``}l(Ce,`getClassesFromDbInfo`);function we(e){return e?.styles??[]}l(we,`getStylesFromDbInfo`);var z=l((e,n,r,i,s,c,l,u)=>{let d=n.id,f=r.get(d),p=Ce(f),m=we(f);if(o.info(`dataFetcher parsedItem`,n,f,m),d!==`root`){let r=M;n.start===!0?r=ee:n.start===!1&&(r=te),n.type!==T&&(r=n.type),F.get(d)||F.set(d,{id:d,shape:r,description:a.sanitizeText(d,t()),cssClasses:`${p} ${oe}`,cssStyles:m});let f=F.get(d);n.description&&(Array.isArray(f.description)?(f.shape=N,f.description.push(n.description)):f.description?.length>0?(f.shape=N,f.description=f.description===d?[n.description]:[f.description,n.description]):(f.shape=M,f.description=n.description),f.description=a.sanitizeTextOrArray(f.description,t())),f.description?.length===1&&f.shape===N&&(f.shape=f.type===`group`?re:M),!f.type&&n.doc&&(o.info(`Setting cluster for XCX`,d,Se(n)),f.type=`group`,f.isGroup=!0,f.dir=Se(n),f.shape=n.type===E?ne:re,f.cssClasses=`${f.cssClasses} ${de} ${c?fe:``}`);let h={labelStyle:``,shape:f.shape,label:f.description,cssClasses:f.cssClasses,cssCompiledStyles:[],cssStyles:f.cssStyles,id:d,dir:f.dir,domId:L(d,I),type:f.type,isGroup:f.type===`group`,padding:8,rx:10,ry:10,look:l};if(h.shape===ne&&(h.label=``),e&&e.id!==`root`&&(o.trace(`Setting node `,d,` to be child of its parent `,e.id),h.parentId=e.id),h.centerLabel=!0,n.note){let e={labelStyle:``,shape:ie,label:n.note.text,cssClasses:ue,cssStyles:[],cssCompilesStyles:[],id:d+_e+`-`+I,domId:L(d,I,me),type:f.type,isGroup:f.type===`group`,padding:t().flowchart.padding,look:l,position:n.note.position},r=d+ve,a={labelStyle:``,shape:ae,label:n.note.text,cssClasses:f.cssClasses,cssStyles:[],id:d+ve,domId:L(d,I,pe),type:`group`,isGroup:!0,padding:16,look:l,position:n.note.position};I++,a.id=r,e.parentId=r,R(i,a,u),R(i,e,u),R(i,h,u);let o=d,c=e.id;n.note.position===`left of`&&(o=e.id,c=d),s.push({id:o+`-`+c,start:o,end:c,arrowhead:`none`,arrowTypeEnd:``,style:D,labelStyle:``,classes:le,arrowheadStyle:O,labelpos:k,labelType:A,thickness:j,look:l})}else R(i,h,u)}n.doc&&(o.trace(`Adding nodes children `),xe(n,n.doc,r,i,s,!c,l,u))},`dataFetcher`),Te=l(()=>{F.clear(),I=0},`reset`),B=`[*]`,Ee=`start`,De=B,Oe=`end`,ke=`color`,Ae=`fill`,je=`bgFill`,Me=`,`;function V(){return new Map}l(V,`newClassesList`);var H=[],Ne=[],Pe=v,U=[],W=V(),Fe=l(()=>({relations:[],states:new Map,documents:{}}),`newDoc`),G={root:Fe()},K=G.root,q=0,Ie=0,Le={LINE:0,DOTTED_LINE:1},Re={AGGREGATION:0,EXTENSION:1,COMPOSITION:2,DEPENDENCY:3},J=l(e=>JSON.parse(JSON.stringify(e)),`clone`),ze=l(e=>{o.info(`Setting root doc`,e),U=e},`setRootDoc`),Be=l(()=>U,`getRootDoc`),Y=l((e,t,n)=>{if(t.stmt===x)Y(e,t.state1,!0),Y(e,t.state2,!1);else if(t.stmt===b&&(t.id===`[*]`?(t.id=n?e.id+`_start`:e.id+`_end`,t.start=n):t.id=t.id.trim()),t.doc){let e=[],n=[],r=0;for(;r<t.doc.length;r++)if(t.doc[r].type===E){let i=J(t.doc[r]);i.doc=J(n),e.push(i),n=[]}else n.push(t.doc[r]);if(e.length>0&&n.length>0){let r={stmt:b,id:f(),type:`divider`,doc:J(n)};e.push(J(r)),t.doc=e}t.doc.forEach(e=>Y(t,e,!0))}},`docTranslator`),Ve=l(()=>(Y({id:`root`},{id:`root`,doc:U},!0),{id:`root`,doc:U}),`getRootDocV2`),He=l(e=>{let n;n=e.doc?e.doc:e,o.info(n),Ue(!0),o.info(`Extract initial document:`,n),n.forEach(e=>{switch(o.warn(`Statement`,e.stmt),e.stmt){case b:X(e.id.trim(),e.type,e.doc,e.description,e.note,e.classes,e.styles,e.textStyles);break;case x:Xe(e.state1,e.state2,e.description);break;case S:et(e.id.trim(),e.classes);break;case C:{let t=e.id.trim().split(`,`),n=e.styleClass.split(`,`);t.forEach(e=>{let t=Z(e);if(t===void 0){let n=e.trim();X(n),t=Z(n)}t.styles=n.map(e=>e.replace(/;/g,``)?.trim())})}break;case w:nt(e.id.trim(),e.styleClass)}});let r=We(),i=t().look;Te(),z(void 0,Ve(),r,H,Ne,!0,i,W),H.forEach(e=>{if(Array.isArray(e.label)){if(e.description=e.label.slice(1),e.isGroup&&e.description.length>0)throw Error(`Group nodes can only have label. Remove the additional description for node [`+e.id+`]`);e.label=e.label[0]}})},`extract`),X=l(function(e,n=T,r=null,i=null,s=null,c=null,l=null,u=null){let d=e?.trim();if(K.states.has(d)?(K.states.get(d).doc||(K.states.get(d).doc=r),K.states.get(d).type||(K.states.get(d).type=n)):(o.info(`Adding state `,d,i),K.states.set(d,{id:d,descriptions:[],type:n,doc:r,note:s,classes:[],styles:[],textStyles:[]})),i&&(o.info(`Setting state description`,d,i),typeof i==`string`&&Ze(d,i.trim()),typeof i==`object`&&i.forEach(e=>Ze(d,e.trim()))),s){let e=K.states.get(d);e.note=s,e.note.text=a.sanitizeText(e.note.text,t())}c&&(o.info(`Setting state classes`,d,c),(typeof c==`string`?[c]:c).forEach(e=>nt(d,e.trim()))),l&&(o.info(`Setting state styles`,d,l),(typeof l==`string`?[l]:l).forEach(e=>rt(d,e.trim()))),u&&(o.info(`Setting state styles`,d,l),(typeof u==`string`?[u]:u).forEach(e=>it(d,e.trim())))},`addState`),Ue=l(function(e){H=[],Ne=[],G={root:Fe()},K=G.root,q=0,W=V(),e||s()},`clear`),Z=l(function(e){return K.states.get(e)},`getState`),We=l(function(){return K.states},`getStates`),Ge=l(function(){o.info(`Documents = `,G)},`logDocuments`),Ke=l(function(){return K.relations},`getRelations`);function Q(e=``){let t=e;return e===B&&(q++,t=`${Ee}${q}`),t}l(Q,`startIdIfNeeded`);function $(e=``,t=T){return e===B?Ee:t}l($,`startTypeIfNeeded`);function qe(e=``){let t=e;return e===De&&(q++,t=`${Oe}${q}`),t}l(qe,`endIdIfNeeded`);function Je(e=``,t=T){return e===De?Oe:t}l(Je,`endTypeIfNeeded`);function Ye(e,n,r){let i=Q(e.id.trim()),o=$(e.id.trim(),e.type),s=Q(n.id.trim()),c=$(n.id.trim(),n.type);X(i,o,e.doc,e.description,e.note,e.classes,e.styles,e.textStyles),X(s,c,n.doc,n.description,n.note,n.classes,n.styles,n.textStyles),K.relations.push({id1:i,id2:s,relationTitle:a.sanitizeText(r,t())})}l(Ye,`addRelationObjs`);var Xe=l(function(e,n,r){if(typeof e==`object`)Ye(e,n,r);else{let i=Q(e.trim()),o=$(e),s=qe(n.trim()),c=Je(n);X(i,o),X(s,c),K.relations.push({id1:i,id2:s,title:a.sanitizeText(r,t())})}},`addRelation`),Ze=l(function(e,n){let r=K.states.get(e),i=n.startsWith(`:`)?n.replace(`:`,``).trim():n;r.descriptions.push(a.sanitizeText(i,t()))},`addDescription`),Qe=l(function(e){return e.substring(0,1)===`:`?e.substr(2).trim():e.trim()},`cleanupLabel`),$e=l(()=>(Ie++,`divider-id-`+Ie),`getDividerId`),et=l(function(e,t=``){W.has(e)||W.set(e,{id:e,styles:[],textStyles:[]});let n=W.get(e);t?.split(Me).forEach(e=>{let t=e.replace(/([^;]*);/,`$1`).trim();if(RegExp(ke).exec(e)){let e=t.replace(Ae,je).replace(ke,Ae);n.textStyles.push(e)}n.styles.push(t)})},`addStyleClass`),tt=l(function(){return W},`getClasses`),nt=l(function(e,t){e.split(`,`).forEach(function(e){let n=Z(e);if(n===void 0){let t=e.trim();X(t),n=Z(t)}n.classes.push(t)})},`setCssClass`),rt=l(function(e,t){let n=Z(e);n!==void 0&&n.styles.push(t)},`setStyle`),it=l(function(e,t){let n=Z(e);n!==void 0&&n.textStyles.push(t)},`setTextStyle`),at={getConfig:l(()=>t().state,`getConfig`),getData:l(()=>{let e=t();return{nodes:H,edges:Ne,other:{},config:e,direction:ye(Ve())}},`getData`),addState:X,clear:Ue,getState:Z,getStates:We,getRelations:Ke,getClasses:tt,getDirection:l(()=>Pe,`getDirection`),addRelation:Xe,getDividerId:$e,setDirection:l(e=>{Pe=e},`setDirection`),cleanupLabel:Qe,lineType:Le,relationType:Re,logDocuments:Ge,getRootDoc:Be,setRootDoc:ze,getRootDocV2:Ve,extract:He,trimColon:l(e=>e&&e[0]===`:`?e.substr(1).trim():e.trim(),`trimColon`),getAccTitle:r,setAccTitle:c,getAccDescription:n,setAccDescription:e,addStyleClass:et,setCssClass:nt,addDescription:Ze,setDiagramTitle:u,getDiagramTitle:i},ot=l(e=>`
defs #statediagram-barbEnd {
    fill: ${e.transitionColor};
    stroke: ${e.transitionColor};
  }
g.stateGroup text {
  fill: ${e.nodeBorder};
  stroke: none;
  font-size: 10px;
}
g.stateGroup text {
  fill: ${e.textColor};
  stroke: none;
  font-size: 10px;

}
g.stateGroup .state-title {
  font-weight: bolder;
  fill: ${e.stateLabelColor};
}

g.stateGroup rect {
  fill: ${e.mainBkg};
  stroke: ${e.nodeBorder};
}

g.stateGroup line {
  stroke: ${e.lineColor};
  stroke-width: 1;
}

.transition {
  stroke: ${e.transitionColor};
  stroke-width: 1;
  fill: none;
}

.stateGroup .composit {
  fill: ${e.background};
  border-bottom: 1px
}

.stateGroup .alt-composit {
  fill: #e0e0e0;
  border-bottom: 1px
}

.state-note {
  stroke: ${e.noteBorderColor};
  fill: ${e.noteBkgColor};

  text {
    fill: ${e.noteTextColor};
    stroke: none;
    font-size: 10px;
  }
}

.stateLabel .box {
  stroke: none;
  stroke-width: 0;
  fill: ${e.mainBkg};
  opacity: 0.5;
}

.edgeLabel .label rect {
  fill: ${e.labelBackgroundColor};
  opacity: 0.5;
}
.edgeLabel {
  background-color: ${e.edgeLabelBackground};
  p {
    background-color: ${e.edgeLabelBackground};
  }
  rect {
    opacity: 0.5;
    background-color: ${e.edgeLabelBackground};
    fill: ${e.edgeLabelBackground};
  }
  text-align: center;
}
.edgeLabel .label text {
  fill: ${e.transitionLabelColor||e.tertiaryTextColor};
}
.label div .edgeLabel {
  color: ${e.transitionLabelColor||e.tertiaryTextColor};
}

.stateLabel text {
  fill: ${e.stateLabelColor};
  font-size: 10px;
  font-weight: bold;
}

.node circle.state-start {
  fill: ${e.specialStateColor};
  stroke: ${e.specialStateColor};
}

.node .fork-join {
  fill: ${e.specialStateColor};
  stroke: ${e.specialStateColor};
}

.node circle.state-end {
  fill: ${e.innerEndBackground};
  stroke: ${e.background};
  stroke-width: 1.5
}
.end-state-inner {
  fill: ${e.compositeBackground||e.background};
  // stroke: ${e.background};
  stroke-width: 1.5
}

.node rect {
  fill: ${e.stateBkg||e.mainBkg};
  stroke: ${e.stateBorder||e.nodeBorder};
  stroke-width: 1px;
}
.node polygon {
  fill: ${e.mainBkg};
  stroke: ${e.stateBorder||e.nodeBorder};;
  stroke-width: 1px;
}
#statediagram-barbEnd {
  fill: ${e.lineColor};
}

.statediagram-cluster rect {
  fill: ${e.compositeTitleBackground};
  stroke: ${e.stateBorder||e.nodeBorder};
  stroke-width: 1px;
}

.cluster-label, .nodeLabel {
  color: ${e.stateLabelColor};
  // line-height: 1;
}

.statediagram-cluster rect.outer {
  rx: 5px;
  ry: 5px;
}
.statediagram-state .divider {
  stroke: ${e.stateBorder||e.nodeBorder};
}

.statediagram-state .title-state {
  rx: 5px;
  ry: 5px;
}
.statediagram-cluster.statediagram-cluster .inner {
  fill: ${e.compositeBackground||e.background};
}
.statediagram-cluster.statediagram-cluster-alt .inner {
  fill: ${e.altBackground?e.altBackground:`#efefef`};
}

.statediagram-cluster .inner {
  rx:0;
  ry:0;
}

.statediagram-state rect.basic {
  rx: 5px;
  ry: 5px;
}
.statediagram-state rect.divider {
  stroke-dasharray: 10,10;
  fill: ${e.altBackground?e.altBackground:`#efefef`};
}

.note-edge {
  stroke-dasharray: 5;
}

.statediagram-note rect {
  fill: ${e.noteBkgColor};
  stroke: ${e.noteBorderColor};
  stroke-width: 1px;
  rx: 0;
  ry: 0;
}
.statediagram-note rect {
  fill: ${e.noteBkgColor};
  stroke: ${e.noteBorderColor};
  stroke-width: 1px;
  rx: 0;
  ry: 0;
}

.statediagram-note text {
  fill: ${e.noteTextColor};
}

.statediagram-note .nodeLabel {
  color: ${e.noteTextColor};
}
.statediagram .edgeLabel {
  color: red; // ${e.noteTextColor};
}

#dependencyStart, #dependencyEnd {
  fill: ${e.lineColor};
  stroke: ${e.lineColor};
  stroke-width: 1;
}

.statediagramTitleText {
  text-anchor: middle;
  font-size: 18px;
  fill: ${e.textColor};
}
`,`getStyles`);export{ot as i,_ as n,be as r,at as t};