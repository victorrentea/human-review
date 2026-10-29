// The Tests tab's matrix renderer. Lifted verbatim from the model-written fragment the
// demo PR shipped (petclinic test-pr, ticket #37) when the matrix stopped being written by a
// model: `semcov.py` now draws the HTML and fills `.rm-data` itself, and this script is
// what turns that data into the card's rows, the selection, the outline and the wires.
// The only edits are marked `semcov:` - who paired a sentence (the script or a model)
// travels with each link and is said on the hover and on the blind-spot box.
(function(){
  // Run again over a fresh matrix when a rebuild swaps the Tests tab into an open served
  // page (`data-hr-swap="rerun"`, the review build's panel swap): the two window listeners
  // the last run left behind are taken off first. Everything else it binds is the matrix.
  if(window.__hrReqmapOff){window.__hrReqmapOff();window.__hrReqmapOff=null;}
  var root=document.currentScript.closest('.reqmap');
  var D=JSON.parse(root.querySelector('.rm-data').textContent);
  var list=root.querySelector('.rm-list'),gap=root.querySelector('.rm-gap'),cur=null;
  function esc(s){return String(s).replace(/[&<>]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
  function where(id){return id.split('/').pop();}
  // `add-visit.component.spec.ts` -> `.spec.ts`, `OwnerListTest.java` -> `.java`: the kind
  // of test is all the row says about its file (Victor, 7 Oct 2026 - "only if it's a
  // feature or a spec.ts or a Java"). The last extension, plus the `.spec`/`.test`
  // before it where there is one; a role like `.component` or `.page` is dropped. The
  // whole repo path rides on the hover.
  function ext(file){
    var m=/((?:\.(?:spec|test|e2e-spec|e2e|cy|it))?\.[A-Za-z0-9]+)$/i.exec(file);
    return m?m[1]:file;
  }

  // Test names arrive as identifiers - `getById_exposesTheAttendingVet` - because that is
  // what the source calls them, and a column of those is read one hump at a time. A name
  // that already has spaces in it (a Gherkin scenario, a Playwright title) is a sentence
  // someone wrote and is left exactly as written; only identifiers are unpacked.
  function human(name){
    if(/\s/.test(name))return name;
    var w=name.replace(/[_\-]+/g,' ')
              .replace(/([a-z0-9])([A-Z])/g,'$1 $2')      // getById  -> get By Id
              .replace(/([A-Z]+)([A-Z][a-z])/g,'$1 $2')   // HTTPServer -> HTTP Server
              .split(/\s+/).filter(Boolean)
              // An all-caps run is an acronym and keeps its case; everything else was
              // only capitalised to mark a word boundary that is now a space.
              .map(function(x){return /^[A-Z0-9]{2,}$/.test(x)?x:x.toLowerCase();});
    return w.length?w[0].charAt(0).toUpperCase()+w[0].slice(1)+(w.length>1?' '+w.slice(1).join(' '):''):name;
  }

  // Every acceptance test the branch offers, UI first, then API - the same order the
  // groups used to impose, now applied once to a list that is always on screen.
  var ORDER={e2e:0,api:1,unit:2};
  // semcov: why a test is on the card comes first - paired with a sentence, written by the
  // branch, aimed at the change, only passing through it (`rank`, from semcov.py). A test
  // about vets that runs the exception advice this PR touched belongs at the bottom, not
  // between the two that prove the ticket.
  function rank(id){var r=D.tests[id].rank;return r===undefined?0:r;}
  // semcov: a paired test sits under the first sentence it covers, in the ticket's
  // reading order (`seq`, `sec`), so the card reads down the spec; one heading per
  // requirement (`D.sections`) in place of "Paired with a sentence of the ticket".
  function seq(id){var q=D.tests[id].seq;return q===undefined?1e9:q;}
  function group(id){var t=D.tests[id];
    return t.sec!==undefined&&D.sections?'s'+t.sec:'r'+rank(id);}
  var ids=Object.keys(D.tests).sort(function(a,b){
    var d=rank(a)-rank(b)||seq(a)-seq(b)||ORDER[D.tests[a].cat]-ORDER[D.tests[b].cat];
    return d||human(D.tests[a].title).localeCompare(human(D.tests[b].title));});
  var groupsShown=ids.length&&D.ranks&&group(ids[0])!==group(ids[ids.length-1]);

  // The first part of a test is the test's own body, so its diff is the test's own story:
  // new, edited, or (no stamp) one this branch left exactly as it found it.
  // The file glyph is VS Code's own `new-file` body, corner already cut for a badge; the
  // plus is its plus. The pencil is drawn rather than borrowed, because the codicon pencil
  // at the size that corner leaves is a few hairlines and disappears - it is a filled bar
  // along the diagonal, with a point at one end and a ferrule near the other.
  var PAGE='<path class="rm-page" d="M9.5 1.1l3.4 3.5.1.4v2h-1V6H8V2H3v11h4v1H2.5l-.5-.5'
          +'v-12l.5-.5h6.7l.3.1zM9 2v3h2.9L9 2z"/>',
      PLUS='<path class="rm-badge" d="M13 16h-1v-3H9v-1h3V9h1v3h3v1h-3v3z"/>',
      PENCIL='<path class="rm-badge" d="M8.65 13.65 13.65 8.65 15.55 10.55 10.55 15.55Z'
            +'M8.65 13.65 10.55 15.55 7.9 16.3Z"/>'
            +'<path class="rm-badge" d="M12.5 9.8 14.4 11.7 13.75 12.35 11.85 10.45Z"/>',
      // The third of the set: the plus turned 45 degrees, because it is the same fact with
      // the sign flipped - a file this branch wrote, changed, or took away.
      CROSS='<path class="rm-badge" d="M16 9.7 15.3 9 12.5 11.8 9.7 9 9 9.7 11.8 12.5'
           +' 9 15.3 9.7 16 12.5 13.2 15.3 16 16 15.3 13.2 12.5Z"/>',
      ICON='<svg viewBox="0 0 16 16" aria-hidden="true">'+PAGE,
      // A test is not a file. The row's stamp used to be the same page glyph with the same
      // badge in its cut corner - right one level down, where the subject really is a file,
      // and wrong here, where the subject is the one test named beside it. Read literally
      // it claimed the branch had added a *file*, which for one test among nine in a file
      // that already existed is simply false, and the `aria-label` said so out loud. So the
      // mark alone, and big: the same three badges blown up to fill the box the page used
      // to occupy (`2x - 16` maps the cut corner onto the whole viewBox), so a reader who
      // has learned plus/pencil/cross downstairs reads them unchanged up here. The page
      // shape stays where it belongs - on the excerpt's badge, which is about a file.
      // `2x - 17` rather than `2x - 16`: the badges are drawn in the 9..16 corner, so
      // doubling them lands 2..16 - hard against two edges and a unit off centre. The odd
      // offset puts them at 1..15, which is the middle of the box they now have to
      // themselves.
      BIG='<svg viewBox="0 0 16 16" aria-hidden="true">'
         +'<g transform="matrix(2 0 0 2 -17 -17)">',
      // [glyph, hover, name — and the name is also the `data-st` the colour hangs off,
      // so a status added here cannot end up wearing another one's badge colour.]
      STAMP={new:[BIG+PLUS+'</g></svg>','New test','new test','new'],
             changed:[BIG+PENCIL+'</g></svg>',
                      'Edited test','edited test','edited'],
             // semcov: its own lines are as they were, but it calls a helper in the same
             // file that this branch rewrote - so the run exercises different code. The
             // pencil and the edited colour (`data-st` is `edited`: it is counted with
             // them); the hover says which helper, from `t.via`.
             helper:[BIG+PENCIL+'</g></svg>',
                     'Edited via a helper',
                     'edited via helper','edited'],
             deleted:[BIG+CROSS+'</g></svg>',
                      'Deleted test','deleted test','deleted'],
             // The one state where the subject IS a file, untouched: the page glyph with no
             // badge on it, same as the excerpt badge one level down says "unchanged".
             // Not blown up like the others - there is nothing in its corner to read.
             unchanged:[ICON+'</svg>',
                        'Unchanged test','unchanged test','unchanged']};
  function stamp(t){
    // The manifest is the authority on what the branch did to a TEST; a part's own diff
    // only knows what happened inside the lines quoted, which is not the same question -
    // a test edited three lines above the excerpt reads as untouched otherwise.
    var st=STAMP[t.status]||STAMP.unchanged;
    // Copy pass (3 Oct 2026): the changed lines its coverage ran (`t.why`) went from the
    // hover; the helper's name (`t.via`) is the one detail kept.
    var tip=st[1]+(t.via?' \u2014 '+esc(t.via).replace(/"/g,'&quot;'):'');
    // The word is gone from the page but not from the accessibility tree: a screen reader
    // reading this row still gets "new test", which is what the glyph is for.
    return '<span class="rm-st" data-st="'+st[3]
      +'" role="img" aria-label="'+st[2]+'" data-tip="'+tip+'">'+st[0]+'</span>';
  }
  // semcov: proven, not inferred - the test's own per-test coverage ran a line this branch
  // changed (`t.why`: which file, which lines). IntelliJ's run-with-coverage shield: the
  // test's run covering the change. Its slot is kept on every row, empty
  // where nothing was proven, so the marks stand in one column beside the stamps.
  var ROUTE='<svg viewBox="0 0 16 16" aria-hidden="true">'
    +'<path d="M8 .9 14.2 3.2v4.6c0 3.6-2.6 6.4-6.2 7.6C4.4 14.2 1.8 11.4 1.8 7.8V3.2Z"/>'
    +'<path class="rm-play" d="M6.4 5.1v5.6l4.4-2.8Z"/></svg>';
  function ran(t){
    if(!t.why)return '<span class="rm-run" aria-hidden="true"></span>';
    return '<span class="rm-run" role="img" aria-label="runs changed code" data-tip="Runs changed code: '
      +esc(t.why).replace(/"/g,'&quot;')+'">'+ROUTE+'</span>';
  }
  // Which of the three the excerpt's own badge is. Keyed off the badge's words, not off
  // `p.diff`: `new file` and `new code` are both `diff:"new"` and are not the same fact -
  // one is a file that did not exist, the other is fresh lines inside one that did.
  function fbadge(p){
    if(!p.badge)return '';
    var kind=/^new file/.test(p.badge)?'new'
            :/^unchanged/.test(p.badge)?'unchanged':'edited',
        mark=kind==='new'?PLUS:kind==='edited'?PENCIL:'';
    // The words the badge used to print stay reachable, in front of the sentence that
    // was already on it: the glyph narrows what is said, it does not delete it.
    return '<span class="rm-fbadge" data-kind="'+kind+'" role="img" aria-label="'
      +esc(p.badge)+'" data-tip="'+esc(p.badge[0].toUpperCase()+p.badge.slice(1))
      +(p.tip?' \u2014 '+esc(p.tip).replace(/"/g,'&quot;'):'')+'">'+ICON+mark+'</svg></span>';
  }

  function partsHtml(tid){
    var t=D.tests[tid],out='';
    t.parts.forEach(function(p){
      var badge=fbadge(p);
      out+='<div class="rm-part"><div class="rm-srcbar">'
         // The name, and the path on hover - the same trade every other quoted block on
         // the page makes. A repo-relative Java path spends five segments on module,
         // `src/main/java` and the org package before it reaches the one word that
         // answers "which file is this?", which is the only question this row exists to
         // answer, and it was wrapping over three lines to say the other five.
         +(function(){
            var cut=p.label.lastIndexOf(':'),
                rel=cut<0?p.label:p.label.slice(0,cut),
                lines=cut<0?'':p.label.slice(cut),
                face=esc(rel.split('/').pop()+lines);
            // semcov: a deleted test's excerpt is its source at the base commit, so it
            // opens there (GitHub), or says how to see it - never a vscode:// into HEAD.
            var tip=esc(p.hrefTip||'Open in VS Code: '+rel).replace(/"/g,'&quot;');
            return p.href?'<a class="srcref" href="'+p.href+'" data-tip="'+tip
              +'" target="_blank" rel="noopener">'+face+'</a>'
              :'<span class="srcref" data-tip="'+tip+'">'+face+'</span>';})()
         // ...and the badge last, because `new file` is a fact ABOUT a file: leading with
         // it made the reader hold it in mind across the whole bar before the bar said
         // which file was new.
         +badge
         +'</div><div class="rm-scroll"><pre class="code'
         +(p.diff==='changed'?' diff-changed':'')+'"><code>';
      // p.html is already-escaped, Pygments-tokenised HTML - one entry per source line.
      // .ln-row / .dm and their colours come from the shared snippet stylesheet.
      p.html.forEach(function(l,i){
        var a=p.add&&p.add[i];
        out+='<span class="ln-row'+(a?' added':'')+'">'
           +(p.marks?'<span class="dm">'+(a?'+':' ')+'</span>':'')
           +'<span class="rm-ln">'+(p.from+i)+'</span>'+l+'</span>\n';});
      out+='</code></pre></div></div>';});
    return out;
  }
  var COVERS={};                       // test id -> [sentence id], inverted from D
  Object.keys(D.sentences).forEach(function(sid){
    (D.sentences[sid].groups||[]).forEach(function(g){g.tests.forEach(function(ev){
      (COVERS[ev.id]=COVERS[ev.id]||[]).push(sid);});});});

  // semcov: the "untouched and unpaired" groups (rank >= D.fold.from) start folded behind
  // one button that counts them - tests about something else that only happen to run a
  // changed line. Folded only when something stays open above them: a card of nothing but
  // pass-through tests shows them, rather than an empty card and a button. The branch's own
  // tests that ran no measured changed line (D.foldOwn, ranks from..to) fold the same way,
  // behind a count of their own: listed, never silently left out (eval run 12). The tests
  // the branch deleted (D.foldGone) fold only past a few (`F.min`): two struck-through rows
  // say more than a button that hides them.
  function mkFold(F,mark,flag){
    if(!F)return null;
    var to=F.to===undefined?Infinity:F.to,f={F:F,n:0,btn:null,mark:mark,flag:flag};
    f.has=function(id){var r=rank(id);return r>=F.from&&r<to;};
    if(ids.some(function(id){return rank(id)<F.from;}))
      ids.forEach(function(id){if(f.has(id))f.n++;});
    if(F.min&&f.n<=F.min)f.n=0;
    return f;
  }
  var FOLDS=[mkFold(D.foldOwn,'own','unown'),mkFold(D.foldGone,'del','undel'),
             mkFold(D.fold,'fold','unfold')]
    .filter(function(f){return f&&f.n>0;});
  function foldOf(id){
    for(var i=0;i<FOLDS.length;i++)if(FOLDS[i].has(id))return FOLDS[i];return null;}
  function setFold(f,open){
    list.dataset[f.flag]=open?'yes':'no';
    f.btn.setAttribute('aria-expanded',open?'true':'false');
    f.btn.textContent=f.n+' '+(f.n===1?f.F.label.replace(/^more tests/,'more test'):f.F.label)
      +' \u2014 '+(open?'hide':'show');
  }
  var lastRank=null;
  ids.forEach(function(id){
    var f=foldOf(id);
    if(f&&!f.btn){
      f.btn=document.createElement('button');
      f.btn.type='button';f.btn.className='rm-fold';f.btn.dataset.which=f.mark;
      list.appendChild(f.btn);setFold(f,false);
    }
    if(groupsShown&&group(id)!==lastRank){
      lastRank=group(id);
      var g=document.createElement('div');
      g.className='rm-tgroup';g.dataset.rank=rank(id);
      if(f)g.dataset[f.mark]='yes';
      g.textContent=lastRank.charAt(0)==='s'?D.sections[lastRank.slice(1)]
                                            :D.ranks[String(rank(id))]||'';
      if(lastRank.charAt(0)==='s')g.dataset.sec='yes';
      list.appendChild(g);
    }
    var t=D.tests[id],row=document.createElement('div');
    row.className='rm-t';row.dataset.open='no';row.dataset.id=id;
    if(f)row.dataset[f.mark]='yes';
    // A test the branch deleted is listed struck through. semcov: it opens on its source
    // as it was at the base commit; only one whose base could not be read stays shut.
    if(t.status==='deleted')row.dataset.gone='yes';
    if(t.status==='deleted'&&!t.parts.length)row.dataset.shut='yes';
    // semcov: switched off (@wip, @Disabled, .skip) - greyed, "never runs", why on its
    // hover; listed, never counted (`discount_never_run`). Not `data-off`: testchapters.js
    // owns that one for the rows its choice hides.
    if(t.off)row.dataset.norun='yes';
    // The location is a link in its own right, not decoration on a button: the row opens
    // the source here, the link opens the file in the editor, and both are wanted.
    var pins=(COVERS[id]||[]).length;
    row.innerHTML='<div class="rm-thead" role="button" tabindex="0" aria-expanded="false">'
      // The other direction, asked for rather than thrown in: this draws the sentences the
      // test pins, and nothing else on the row does.
      +'<button class="rm-link" type="button" aria-pressed="false"'
      +(pins?'':' disabled')
      +(pins?' data-tip="Show the '+pins+' sentence'+(pins===1?'':'s')+' it covers"':'')
      +' aria-label="outline the sentences this test pins">'
      +'<svg class="rm-fan" viewBox="0 0 12 14" aria-hidden="true">'
      +'<path d="M7 7H11.2"/><path d="M7 7H1.2"/>'
      +'<path d="M7 7C4.6 7 4.6 1.7 1.2 1.7"/>'
      +'<path d="M7 7C4.6 7 4.6 12.3 1.2 12.3"/></svg></button>'
      +'<span class="rm-cat" data-cat="'+t.cat+'">'+esc(D.cats[t.cat])+'</span>'
      // The arrow leads the title rather than closing the row: it is the control, and a
      // control at the far right of a variable-width row is a control nobody finds.
      +'<span class="rm-chev">&#9654;</span>'
      +'<span class="rm-tt">'+esc(human(t.title))+'</span>'
      // The link lands on the test's own declaration, not on the first line of the
      // window quoted around it - a window often opens on the context above the test.
      +(function(){
         // Where it lived. A link while there is something to open; a deleted test has no
         // file left in this checkout, and a dead vscode:// URL is the one thing this page
         // never emits, so it keeps the location and loses the link.
         var href=t.href||(t.parts&&t.parts[0]&&t.parts[0].href),
             face=esc(ext((t.where||where(id)).replace(/:\d+(?:[-\u2013]\d+)?$/,''))),
             // The id is the repo path and line (`@base` marks a deleted test's key).
             path=esc(id.replace(/@base$/,'')).replace(/"/g,'&quot;')+' \u2014 ';
         // semcov: a deleted test's href is its blob at the base commit, and says so.
         return href?'<a class="rm-tw srcref" href="'+href+'" data-tip="'+path+esc(t.hrefTip||'Open in VS Code')+'" target="_blank" rel="noopener">'
                     +face+'</a>'
                   :'<span class="rm-tw srcref tgone" data-tip="'+path
                     +esc(t.goneTip||'the file is gone').replace(/"/g,'&quot;')+'">'
                     +face+'</span>';})()
      // The stamp closes the row, on the right of the file it is about - the same order
      // the source bar inside the row reads in, and the same reason: "new" is a fact ABOUT
      // a file, so it comes after the file has been named. Between the title and the
      // location it sat between two things that belong together and was read as part of
      // the sentence naming the test.
      +(t.off?'<span class="rm-norun" data-tip="'+esc(t.off).replace(/"/g,'&quot;')
              +'">never runs</span>':'')
      +ran(t)+stamp(t)+'</div>'
      +'<div class="rm-tbody"><div class="rm-tinner"></div></div>';
    // Not a button if there is nothing to press: a row that cannot open should not tell a
    // screen reader that it expands, nor take a tab stop to do nothing with.
    if(row.dataset.shut==='yes'){
      var h=row.querySelector('.rm-thead');
      h.removeAttribute('role');h.removeAttribute('tabindex');
      h.removeAttribute('aria-expanded');
    }
    list.appendChild(row);
  });

  function outline(id){
    Array.prototype.forEach.call(root.querySelectorAll('.rm-f[data-by]'),function(f){
      f.removeAttribute('data-by');});
    (COVERS[id]||[]).forEach(function(sid){
      var f=root.querySelector('.rm-f[data-s="'+sid+'"]');
      if(f)f.setAttribute('data-by','yes');});
    wired=id;redraw();
  }

  // ---- the wires -------------------------------------------------------------------
  // An outline says WHICH sentences a test pins; on a ticket four screens long it never
  // says which of them belong together, and with the card sticky the row that pinned them
  // is usually nowhere near any of them. So the relation is drawn: one wire per sentence,
  // from a dot on the ticket's own border across the gutter to the row it came from.
  // Geometry only - nothing here decides anything, it redraws what `outline` already
  // decided, from wherever the two columns currently happen to be.
  var wrap=root.querySelector('.rm-body'),
      ticket=root.querySelector('.rm-ticket'),
      sideCol=root.querySelector('.rm-side'),
      card=root.querySelector('.rm-code'),
      svg=document.createElementNS('http://www.w3.org/2000/svg','svg'),
      wired=null,pending=false;
  svg.setAttribute('class','rm-wires');svg.setAttribute('aria-hidden','true');
  wrap.appendChild(svg);

  // Each column scrolls on its own (tests.py REQMAP_CSS) - under its header strip, which
  // stays put: what scrolls is the ticket's prose (`.rm-issue`) and the card's list
  // (`.rm-list`), not the column. A click on one side brings the other side's matches into
  // view: as many of them as fit, the first at the top. Nothing moves when they are all
  // on screen already, or when nothing in the column scrolls (stacked, under 900px, the
  // page scrolls both).
  var textCol=root.querySelector('.rm-text');
  function scroller(pane){
    if(!pane)return null;
    var c=[pane].concat(Array.prototype.slice.call(pane.querySelectorAll('.rm-issue,.rm-list')));
    for(var i=0;i<c.length;i++)
      if(c[i].scrollHeight>c[i].clientHeight+1&&/auto|scroll/.test(getComputedStyle(c[i]).overflowY))
        return c[i];
    return null;
  }
  // The part of a column a reader can see things in: its scroller, or the column itself.
  function view(pane){var sc=scroller(pane);return (sc||pane).getBoundingClientRect();}
  function bringIn(pane,els){
    els=els.filter(function(e){return e&&e.getClientRects().length;});
    var sc=scroller(pane);
    if(!sc||!els.length)return;
    // A heading that sticks to the scroller's top edge (the card's E2E/API/UNIT chapters)
    // covers the first rows under it: the target lands below it, not behind it.
    var ch=els[0].closest&&els[0].closest('.rm-ch'),hh=ch&&ch.querySelector('.rm-chh'),
        pad=hh?hh.offsetHeight:0;
    var p=sc.getBoundingClientRect(),top=Infinity,inView=true;
    els.forEach(function(e){var r=e.getBoundingClientRect();
      top=Math.min(top,r.top);if(r.top<p.top+pad||r.bottom>p.bottom)inView=false;});
    if(!inView)sc.scrollBy({top:top-p.top-pad-8,behavior:'smooth'});
  }

  function draw(){
    var row=wired&&list.querySelector('.rm-t[data-link=yes]');
    if(!row&&cur&&list.dataset.sel==='yes'){drawSel();return;}
    if(!row){svg.textContent='';wrap.dataset.wired='no';return;}
    var base=wrap.getBoundingClientRect(),t=ticket.getBoundingClientRect(),
        sd=sideCol.getBoundingClientRect(),c=card.getBoundingClientRect();
    // Under 900px the two columns stack, one above the other. There is no gutter to draw
    // in and no left-to-right relation to draw; the outline carries it alone.
    if(sd.left<t.right+8){svg.textContent='';wrap.dataset.wired='no';return;}
    var x0=t.right-base.left,ax=sd.left-base.left,
        // The row's TITLE, not the row: an open row is its header plus however many
        // screens of source it quotes, and the middle of that is somewhere down in the
        // code. The wire has to hang off the name of the test, which is the part that
        // stays put and the part the reader is looking at.
        r=row.querySelector('.rm-thead').getBoundingClientRect(),
        // The card scrolls inside itself, so the row can be above or below what it shows.
        // The wire then lands on the card's edge at the point the row left it, which is
        // the direction the reader has to scroll.
        sv=view(sideCol),
        ay=Math.min(Math.max((r.top+r.bottom)/2,Math.max(c.top,sv.top)+8),
                    Math.min(c.bottom,sv.bottom)-8)-base.top,d='';
    (COVERS[wired]||[]).forEach(function(sid){
      var f=root.querySelector('.rm-f[data-s="'+sid+'"]');if(!f)return;
      // A sentence that wraps is several rectangles, and the wire leaves from the middle
      // of all of them together - the middle of the block of text, not of its last line.
      var rects=f.getClientRects(),top=Infinity,bot=-Infinity;
      if(!rects.length)return;
      Array.prototype.forEach.call(rects,function(k){
        top=Math.min(top,k.top);bot=Math.max(bot,k.bottom);});
      // The wire lands on the ticket's own border, on the line the sentence sits on, and
      // the dot is what makes that a landing rather than a line running out of page.
      // The ticket scrolls inside its column too: a sentence above or below what it shows
      // gets its wire on the column's edge, the way the card's rows do.
      var tc=textCol?view(textCol):t,
          mid=Math.min(Math.max((top+bot)/2,tc.top+8),tc.bottom-8)-base.top,
          // The bend grows with the drop, but never past the gutter: a control point
          // further out than the gap is wide pulls the curve backwards mid-way, over the
          // card it is leaving - in a 20px gutter any drop over ~125px did that.
          dx=Math.min(Math.max(12,Math.abs(ay-mid)*0.16),Math.max(4,(ax-x0)*0.9));
      d+='<path class="rm-wire" d="M'+x0+' '+mid+'C'+(x0+dx)+' '+mid+' '
        +(ax-dx)+' '+ay+' '+ax+' '+ay+'"/>'
        +'<circle class="rm-dot" cx="'+x0+'" cy="'+mid+'" r="3"/>';
    });
    svg.innerHTML=d?d+'<circle class="rm-hub" cx="'+ax+'" cy="'+ay+'" r="3"/>':'';
    wrap.dataset.wired=d?'yes':'no';
  }

  // The first direction, drawn too (Victor, 19 Oct 2026): a picked sentence wires itself to
  // every test that covers it, whichever chapter it is filed in - one dot on the ticket's
  // border, one wire per test. Where the test cannot be seen the wire still says where it
  // is: into its chapter's heading when the chapter is closed, and onto the card's top or
  // bottom edge when the row is scrolled past, where a `▲ 3` / `▼ 2` counts the rows that
  // way. A kind with no test for the sentence says "none" on its heading (testchapters.js).
  function drawSel(){
    var off=function(){svg.textContent='';wrap.dataset.wired='no';};
    var base=wrap.getBoundingClientRect(),t=ticket.getBoundingClientRect(),
        sd=sideCol.getBoundingClientRect(),c=card.getBoundingClientRect();
    if(sd.left<t.right+8)return off();
    var rects=cur.getClientRects();if(!rects.length)return off();
    var top=Infinity,bot=-Infinity;
    Array.prototype.forEach.call(rects,function(k){top=Math.min(top,k.top);bot=Math.max(bot,k.bottom);});
    var tc=textCol?view(textCol):t,x0=t.right-base.left,ax=sd.left-base.left,
        mid=Math.min(Math.max((top+bot)/2,tc.top+8),tc.bottom-8)-base.top,
        sv=view(sideCol),lv=list.getBoundingClientRect(),
        lo=Math.max(lv.top,c.top,sv.top),hi=Math.min(lv.bottom,c.bottom,sv.bottom);
    if(hi-lo<16)return off();
    var ends={},up=0,down=0;
    Array.prototype.forEach.call(list.querySelectorAll('.rm-t[data-hit=yes]'),function(r){
      var sec=r.closest('.rm-ch'),head=sec&&sec.querySelector('.rm-chh'),
          el=r.querySelector('.rm-thead'),floor=lo;
      if(sec&&sec.hidden)return;
      if(!el.getClientRects().length){
        // Not laid out: in a closed chapter, its heading is the place; in an open one it is
        // folded out of the list, and there is nowhere to point.
        if(!sec||sec.dataset.open==='yes'||!head)return;
        el=head;
      }else if(head){
        floor=Math.max(lo,head.getBoundingClientRect().bottom);   // behind its stuck heading
      }
      var b=el.getBoundingClientRect(),y=(b.top+b.bottom)/2;
      if(el!==head&&y<floor)up++;
      else if(y>hi)down++;
      else ends[Math.round(y)]=1;
    });
    var d='',ys=Object.keys(ends).map(Number);
    if(up)ys.push(lo+9);
    if(down)ys.push(hi-9);
    ys.forEach(function(y){
      var ay=y-base.top,dx=Math.min(Math.max(12,Math.abs(ay-mid)*0.16),Math.max(4,(ax-x0)*0.9));
      d+='<path class="rm-wire" d="M'+x0+' '+mid+'C'+(x0+dx)+' '+mid+' '+(ax-dx)+' '+ay+' '+ax+' '+ay+'"/>'
        +'<circle class="rm-hub" cx="'+ax+'" cy="'+ay+'" r="3"/>';
    });
    function edge(n,y,glyph){
      // Astride the card's border, where the wire lands: half in the gutter, half over the
      // rows' blank wire column, so it covers no name and no heading's badge.
      var w=n>9?34:28;
      return '<g class="rm-edge"><rect x="'+(ax-w/2)+'" y="'+(y-base.top-8)+'" width="'+w
        +'" height="16" rx="8"/><text x="'+ax+'" y="'+(y-base.top+4)+'">'+glyph+'\u2009'+n
        +'</text></g>';
    }
    if(up)d+=edge(up,lo+9,'\u25B2');
    if(down)d+=edge(down,hi-9,'\u25BC');
    if(!d)return off();
    svg.innerHTML=d+'<circle class="rm-dot" cx="'+x0+'" cy="'+mid+'" r="3"/>';
    wrap.dataset.wired='yes';
  }

  // Every redraw is a full remeasure, and the things that move the endpoints are the page
  // scrolling, the card scrolling inside itself, the window resizing and a row opening.
  // One frame's worth of coalescing keeps that off the scroll path.
  function redraw(){
    if(pending)return;pending=true;
    requestAnimationFrame(function(){pending=false;draw();});
  }
  addEventListener('scroll',redraw,true);   // capture: the card's own scroll does not bubble
  addEventListener('resize',redraw);
  window.__hrReqmapOff=function(){
    removeEventListener('scroll',redraw,true);removeEventListener('resize',redraw);};
  if(window.ResizeObserver)new ResizeObserver(redraw).observe(wrap);
  // The accordion animates its height, so the rows under it are still moving after the
  // click: follow them to where they land.
  list.addEventListener('transitionend',redraw);
  function toggle(row){
    if(row.dataset.shut==='yes')return;   // there is no source to show
    var open=row.dataset.open==='yes';
    // An accordion, not a stack: opening one closes the rest, so the list never turns
    // into three screens of source with the row you wanted somewhere in the middle.
    Array.prototype.forEach.call(list.querySelectorAll('.rm-t'),function(o){
      o.dataset.open='no';
      o.querySelector('.rm-thead').setAttribute('aria-expanded','false');});
    if(open){redraw();return;}
    var body=row.querySelector('.rm-tinner');
    if(!body.innerHTML)body.innerHTML=partsHtml(row.dataset.id);
    row.dataset.open='yes';
    row.querySelector('.rm-thead').setAttribute('aria-expanded','true');
    redraw();
  }

  // The other direction, drawn on demand: which sentences does THIS test pin. One row at a
  // time - two rows outlining into the same paragraph and the outline stops meaning
  // anything - and it releases the sentence selection first, because "these tests cover
  // that sentence" and "that test pins these sentences" are two different relations and
  // showing both in the same green leaves the reader working out which is which.
  function link(row){
    var on=row.dataset.link==='yes';
    clearSel();
    Array.prototype.forEach.call(list.querySelectorAll('.rm-t'),function(o){
      o.dataset.link='no';
      o.querySelector('.rm-link').setAttribute('aria-pressed','false');});
    if(on){outline(null);return;}
    row.dataset.link='yes';
    row.querySelector('.rm-link').setAttribute('aria-pressed','true');
    outline(row.dataset.id);
    bringIn(textCol,(COVERS[row.dataset.id]||[]).map(function(sid){
      return root.querySelector('.rm-f[data-s="'+sid+'"]');}));
  }

  // Everything the sentence -> tests direction put on the page, taken back off it.
  function clearSel(){
    if(cur){cur.removeAttribute('aria-current');cur=null;}
    delete list.dataset.sel;
    Array.prototype.forEach.call(list.querySelectorAll('.rm-t'),function(row){
      delete row.dataset.hit;
      var b=row.querySelector('.rm-cat');if(b)delete b.dataset.strength;
      row.querySelector('.rm-thead').removeAttribute('data-tip');});
    gap.innerHTML='';gap.hidden=true;rejMark(null);redraw();
  }
  list.addEventListener('click',function(e){
    var fb=e.target.closest('.rm-fold');                // semcov: show/hide the folded rest
    if(fb){FOLDS.forEach(function(f){if(f.btn===fb)setFold(f,list.dataset[f.flag]!=='yes');});
      redraw();return;}
    var l=e.target.closest('.rm-link');                 // draws, never opens
    if(l){link(l.closest('.rm-t'));return;}
    if(e.target.closest('.rm-tw'))return;              // the editor link is not a toggle
    var h=e.target.closest('.rm-thead');if(h)toggle(h.parentNode);});
  list.addEventListener('keydown',function(e){
    // A <button> already fires its own click on Enter and Space; letting the row's handler
    // see the key as well would open the source at the same time.
    if(e.target.closest&&e.target.closest('.rm-link'))return;
    var h=e.target.closest&&e.target.closest('.rm-thead');
    if(h&&(e.key==='Enter'||e.key===' ')){e.preventDefault();toggle(h.parentNode);}});

  // semcov: the decision a narrowed sentence rests on, named and linked where it was
  // recorded (`proposal.md:86`) - "Recorded: <the whole line>" left the reader to find it.
  function recorded(s){
    if(!s.decisionWhere)return 'Recorded: '+esc(s.decision);
    var w=s.decisionHref?'<a class="srcref" href="'+esc(s.decisionHref).replace(/"/g,'&quot;')
      +'" data-tip="Open in VS Code" target="_blank" rel="noopener">'+esc(s.decisionWhere)+'</a>'
      :esc(s.decisionWhere);
    return 'Recorded in '+w+': '+esc(s.decisionQuote||s.decision);
  }
  // semcov: the links a model read and turned down. They were a second box under the
  // blind spot, and on a short screen that box ate the ticket the reader was selecting
  // from (Victor, 8 Oct 2026: "it obscures the vision"). Now one "!" at the end of the
  // colour legend, there only when the selected sentence has any; the list is its tip.
  function rejMark(rej){
    var lg=root.querySelector('.rm-legend');if(!lg)return;
    var b=lg.querySelector('.rm-rejmark');
    if(!rej||!rej.length){if(b)b.hidden=true;return;}
    if(!b){b=document.createElement('button');b.type='button';b.className='rm-rejmark';
      b.textContent='!';lg.appendChild(b);}
    b.setAttribute('aria-label',rej.length+' keyword matches rejected by AI');
    b.setAttribute('data-tip-html','<p class="tipfoot">Matched on words, rejected by 🤖</p>'
      +'<ul class="tiplist prose">'+rej.map(function(r){
        return '<li><code>'+esc(where(r.id))+'</code> \u2014 '+esc(r.why)+'</li>';}).join('')+'</ul>');
    b.hidden=false;
  }
  function open(sid){
    var s=D.sentences[sid];if(!s)return;
    // Clicking the sentence you already picked puts the ticket back the way it was. There
    // was no way out of a selection except by making another one, so the column stayed
    // sorted into "these prove it" and a dimmed rest long after the reader had finished
    // asking - and the dimming is the page's answer to a question, not its resting state.
    if(cur&&cur.dataset.s===sid){clearSel();return;}
    if(cur)cur.removeAttribute('aria-current');
    cur=root.querySelector('.rm-f[data-s="'+sid+'"]');cur.setAttribute('aria-current','true');
    // Which of the nine cover this sentence, and how hard each one pins it.
    var hit={};
    (s.groups||[]).forEach(function(g){g.tests.forEach(function(ev){
      hit[ev.id]={cat:g.cat,strength:ev.off?'off':ev.strength,why:ev.why,by:ev.by,
                  off:ev.off};});});
    list.dataset.sel='yes';outline(null);
    Array.prototype.forEach.call(list.querySelectorAll('.rm-t'),function(row){
      var h=hit[row.dataset.id],b=row.querySelector('.rm-cat');
      row.dataset.hit=h?'yes':'no';
      row.dataset.link='no';
      row.querySelector('.rm-link').setAttribute('aria-pressed','false');
      row.dataset.open='no';
      row.querySelector('.rm-thead').setAttribute('aria-expanded','false');
      if(h){b.dataset.strength=h.strength;row.querySelector('.rm-thead')
        .setAttribute('data-tip',h.off?'never runs \u2014 not counted: '+(D.tests[row.dataset.id].off||'')
          :(h.by==='model'?'\uD83E\uDD16 ':'')+h.strength+' — '+(h.why||''));}
      else{delete b.dataset.strength;row.querySelector('.rm-thead').removeAttribute('data-tip');}
    });
    // A gap is a criticism of the REQUIREMENT, so it is shown beside the requirement.
    // "Blind spot" alone left the reader asking whose fault it is - the tests', for not
    // reaching a claim the ticket makes, or the ticket's, for never making the claim.
    // The two are different findings and go to different people.
    var kind=s.gapKind==='requirement'?'in the requirement':'in the tests';
    gap.dataset.kind=s.gapKind||'tests';
    var head=s.cov==='narrowed'?'Narrowed on purpose':'Blind spot';
    gap.innerHTML=s.gap?'<h4>'+head+' <span class="rm-gapkind">'+kind+'</span>'
      +(s.by==='model'?'<sup class="rm-ai" data-tip="as inferred by AI">🤖</sup>':'')+'</h4><p>'+esc(s.gap)+'</p>'
      +(s.decision?'<p class="rm-dec">'+recorded(s)+'</p>':''):'';
    gap.hidden=!s.gap;
    rejMark(s.rejected);
    redraw();
    bringIn(sideCol,Array.prototype.filter.call(list.querySelectorAll('.rm-t[data-hit=yes]'),
      function(r){return r.dataset.catoff!=='yes';}));
  }
  root.addEventListener('click',function(e){
    var f=e.target.closest('.rm-f');if(f&&root.contains(f))open(f.dataset.s);});
  root.addEventListener('keydown',function(e){
    var f=e.target.closest&&e.target.closest('.rm-f');
    if(f&&(e.key==='Enter'||e.key===' ')){e.preventDefault();open(f.dataset.s);}});

  // The hover on a sentence shows the badges themselves, not their names in prose, so
  // the tip and the list on the right are visibly the same two concepts - then says what
  // clicking will do, because nothing else on the page announces that.
  Array.prototype.forEach.call(root.querySelectorAll('.rm-f[data-s]'),function(f){
    var s=D.sentences[f.dataset.s];if(!s)return;
    // semcov: a test that never runs is no badge's count; it is said apart, after them.
    var norun=0;
    var tip=(s.groups||[]).map(function(g){
      var n=g.tests.filter(function(ev){if(ev.off)norun++;return !ev.off;}).length;
      return n?'<span class="rm-cat" data-cat="'+g.cat+'">'+esc(D.cats[g.cat])
        +'</span><span class="rm-n">\u00D7'+n+'</span>':'';}).join('');
    tip=tip?tip
           :'<span class="rm-do">no test \u00B7 click for what is missing</span>';
    if(norun)tip+='<span class="rm-do"> \u00B7 '+norun+' never run'+(norun===1?'s':'')+'</span>';
    // semcov: who paired it, after the badges - the script's evidence or a model's reading.
    tip+=s.cov==='unmapped'?'<span class="rm-do"> not paired yet \u00B7 run the 🤖</span>'
        :s.cov==='unconfirmed'?'<span class="rm-do"> \u00B7 unconfirmed \u00B7 run the 🤖</span>'
        :s.cov==='narrowed'?'<span class="rm-do"> \u00B7 narrowed by '
          +(s.decisionName?esc(s.decisionName):'a recorded decision')+'</span>'
        // A model's pairing is its opinion, and says so in one word (Victor, 20 Oct 2026).
        :'<span class="rm-do">'+(s.by==='model'?' \u2013 🤖 opinion':' \u00B7 paired by script')+'</span>';
    // What a partial or narrowed sentence lacks, in the matching pass's own words (its
    // `gap`): the hatch says "not all of it", and only the hover can say which part
    // without a click (Victor, 5 Oct 2026).
    if((s.cov==='partly'||s.cov==='narrowed')&&s.gap)
      tip+='<span class="rm-gaptip">'+esc(s.gap)+'</span>';
    f.setAttribute('data-tip-html',tip);
    f.removeAttribute('data-tip');
  });
})();
