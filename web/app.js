'use strict';
const $ = selector => document.querySelector(selector);
const paths = {
  home:'<path d="m3 10 9-7 9 7v10H3z"/><path d="M9 20v-7h6v7"/>',
  compass:'<circle cx="12" cy="12" r="9"/><path d="m16 8-3 5-5 3 3-5z"/>',
  bookmark:'<path d="M6 3h12v18l-6-4-6 4z"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
  down:'<path d="M12 4v16m-6-6 6 6 6-6"/>',
  up:'<path d="M12 20V4m-6 6 6-6 6 6"/>',
  play:'<path d="m8 4 12 8-12 8z"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 6-6 4 4 3-3 5 5"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 6M4 12l2 6a7 7 0 0 0 12-1"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
};
function icon(name) { const e=document.createElement('span'); e.innerHTML=`<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.compass}</svg>`; return e; }
document.querySelectorAll('[data-icon]').forEach(el=>el.replaceWith(icon(el.dataset.icon)));
function el(tag,cls,text) {const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n;}
function button(text,cls,fn) {const b=el('button',cls,text);b.type='button';b.addEventListener('click',fn);return b;}
function safeURL(value) {try {if(typeof value!=='string'||!value)return null;const u=new URL(value,location.origin);return ['https:','http:'].includes(u.protocol)?u.href:null;}catch{return null;}}
function external(text,url,cls='') {const a=el('a',cls,text);a.href=safeURL(url)||'https://www.reddit.com/';a.target='_blank';a.rel='noopener noreferrer';return a;}
const suggestions=[
  {subreddit:'pics',description:'A world worth looking at. One photo at a time.'},
  {subreddit:'aww',description:'A little softer. A little fluffier. A lot more smiles.'},
  {subreddit:'gaming',description:'New worlds, familiar favorites, and one more level.'},
  {subreddit:'earthporn',description:'The planet showing off. No filter needed.'},
  {subreddit:'oddlysatisfying',description:'Everything falling into place, just right.'},
  {subreddit:'space',description:'For the things that make you look up.'},
];
let saved;
try {saved=JSON.parse(localStorage.getItem('swiiper:saved')||'[]'); if(!Array.isArray(saved))saved=[];}catch{saved=[];}
saved=saved.filter(p=>p&&typeof p.id==='string'&&typeof p.subreddit==='string').slice(0,500);
let communities=[],posts=[],after=null,loading=false,ended=false,epoch=0,kind='all',activeFeed='all',page='home',searchTerm='';
let viewerIndex=0,hls=null,video=null,toastTimer,jobTimer,searchTimer,lastTrigger=null,viewerSequence=0;
let controller=new AbortController();
let streamIndex=0;
let streamNavigating=false;
let streamWheelTotal=0;
let streamWheelTimer=null;
let streamGestureUntil=0;
const streamPlayers=new Map();

function showToast(message){$('#toast').textContent=message;$('#toast').classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.remove('visible'),2400);}
function updateSaved(){ $('#saved-count').textContent=String(saved.length);document.querySelectorAll('[data-save-id]').forEach(b=>{const yes=saved.some(p=>p.id===b.dataset.saveId);b.classList.toggle('saved',yes);b.setAttribute('aria-label',yes?'Unsave post':'Save post');b.setAttribute('aria-pressed',String(yes));});}
function toggleSaved(post){const i=saved.findIndex(p=>p.id===post.id);if(i<0)saved.unshift(post);else saved.splice(i,1);saved=saved.slice(0,500);try{localStorage.setItem('swiiper:saved',JSON.stringify(saved));}catch{showToast('Your browser could not save this collection.');return;}updateSaved();showToast(i<0?'Saved for your next rabbit hole.':'Removed from saved.');if(page==='saved'&&!$('#viewer').open)renderSaved();}
async function request(path,options={}){const response=await fetch(path,{signal:controller.signal,...options});let data;try{data=await response.json();}catch{throw new Error('The service returned an unreadable response.');}if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Unable to connect. Please try again.');return data;}
function age(timestamp){const seconds=Math.max(0,(Date.now()-Date.parse(timestamp))/1000);if(!Number.isFinite(seconds))return 'Recently';if(seconds<3600)return `${Math.max(1,Math.floor(seconds/60))}m ago`;if(seconds<86400)return `${Math.floor(seconds/3600)}h ago`;return `${Math.floor(seconds/86400)}d ago`;}
function navigate(hash){if(location.hash===hash)renderRoute();else location.hash=hash;}
function communityLink(name){return '#/r/'+encodeURIComponent(name);}
async function loadCommunities(){try{const data=await request('/api/v1/subreddits');communities=data.subreddits||[];renderSidebar();renderChips();if(page==='explore')renderExplore();}catch(e){if(e.name!=='AbortError'&&page==='explore'){$('#community-subtitle').textContent='Community counts are unavailable. You can still search by name.';renderExplore();}}}
function renderSidebar(){const list=$('#sidebar-communities');list.replaceChildren();for(const c of communities.slice(0,8)){const a=el('a');a.href=communityLink(c.subreddit);a.append(el('span','community-avatar',c.subreddit[0].toUpperCase()),el('span','',`r/${c.subreddit}`));list.append(a);}}
function renderChips(){const area=$('#community-chips');area.replaceChildren();if(page==='saved'){area.append(el('span','subtle','Your favorites, in one place.'));return;}const list=[{subreddit:'all'},...communities.slice(0,5)];for(const c of list){const b=button(c.subreddit==='all'?'For you':`r/${c.subreddit}`,'chip'+(activeFeed===c.subreddit?' active':''),()=>navigate(c.subreddit==='all'?'#/home':communityLink(c.subreddit)));area.append(b);}}
function setKind(next){if(kind===next)return;kind=next;document.querySelectorAll('[data-kind]').forEach(b=>b.classList.toggle('active',b.dataset.kind===kind));if(page==='saved')renderSaved();else resetFeed();}
function skeletons(){const grid=$('#feed-grid');grid.replaceChildren();for(let i=0;i<6;i++)grid.append(el('div','skeleton'));}
function emptyState(title,description,action){const box=el('div','empty-state');box.append(icon(page==='saved'?'bookmark':'compass'),el('h3','',title),el('p','',description));if(action)box.append(action);$('#feed-grid').replaceChildren(box);}
function resetFeed(){clearStream();epoch++;controller.abort();controller=new AbortController();clearTimeout(jobTimer);posts=[];after=null;ended=false;loading=false;$('#feed-state').textContent='';$('#load-more').hidden=true;skeletons();$('#feed-grid').scrollTop=0;loadPage();}
async function loadPage(){if(loading||ended)return;loading=true;const token=epoch;$('#feed-state').textContent=posts.length?'Finding a few more…':'';$('#load-more').disabled=true;try{const query=new URLSearchParams({subreddit:activeFeed,limit:'18',kind});if(after)query.set('after',after);const data=await request('/api/v1/feed?'+query);if(token!==epoch)return;if(!Array.isArray(data.posts))throw new Error('This feed is temporarily unavailable.');if(!posts.length)$('#feed-grid').replaceChildren();const ids=new Set(posts.map(p=>p.id));for(const post of data.posts){if(!post||ids.has(post.id))continue;ids.add(post.id);posts.push(post);if(page!=='home')renderCard(post,posts.length-1);}after=data.after;ended=!after;$('#feed-state').textContent=page!=='home'&&ended&&posts.length?'You’re all caught up. Curiosity never ends, though.':'';$('#load-more').hidden=ended||page==='home';$('#start-swiping').disabled=!posts.length;if(!posts.length){const noVideo=kind==='video';emptyState(noVideo?'No playable videos here yet.':'Your next discovery starts here.',noVideo?'Try another community, switch to All, or refresh this community.':'Load this community to bring its latest posts into your feed.',activeFeed==='all'?button('Explore communities','button primary',()=>navigate('#/explore')):button('Load latest posts','button primary',()=>loadCommunity(activeFeed)));}if(page==='home'&&posts.length&&!$('#feed-grid .stream-card'))renderStreamWindow();updateSaved();}catch(e){if(token!==epoch||e.name==='AbortError')return;$('#feed-state').textContent='';if(!posts.length)emptyState('A little pause in your scroll.',e.message,button('Try again','button primary',resetFeed));else{$('#feed-state').textContent=e.message;$('#load-more').hidden=false;}}finally{if(token===epoch){loading=false;$('#load-more').disabled=false;}}}
function setHero(){const images=posts.filter(p=>p.mediaType==='image'&&safeURL(p.mediaUrl));for(const [id,i] of [['#hero-front',1],['#hero-back',4]]){const image=images[i]||images[0];if(image&&$(id))$(id).style.backgroundImage=`url(${JSON.stringify(safeURL(image.mediaUrl))})`;}}
function renderCard(post,index){if(page==='home'){renderStreamCard(post,index);return;}const card=el('article','post-card');const image=button('','post-image',()=>openViewer(index,image));image.setAttribute('aria-label',`${post.mediaType==='video'?'Play':'View'}: ${post.title}`);let src=safeURL(post.thumbnail)||(post.mediaType==='image'?safeURL(post.mediaUrl):null);if(src){const img=el('img');img.src=src;img.alt=post.title||'';img.loading='lazy';img.decoding='async';img.addEventListener('error',()=>img.replaceWith(el('div','placeholder-art','r/')),{once:true});image.append(img);}else image.append(el('div','placeholder-art','r/'));const tag=el('span','media-tag');tag.append(icon(post.mediaType==='video'?'play':post.mediaType==='image'?'image':'arrow'),el('span','',post.mediaType==='video'?'VIDEO':post.mediaType==='image'?'PHOTO':'LINK'));image.append(tag,el('span','card-sub',post.subreddit));if(post.mediaType==='video'){const play=el('span','play-badge');play.append(icon('play'));image.append(play);}const save=button('','card-save',()=>toggleSaved(post));save.dataset.saveId=post.id;save.append(icon('bookmark'));const info=el('div','post-info');const title=el('h3','',post.title);const meta=el('div','post-meta');meta.append(el('span','',age(post.timestamp)),el('span','','·'),el('span','',post.mediaType==='video'?'Press play. Stay a while.':'From the community'),el('span','meta-arrow','↗'));info.append(title,meta);card.append(image,save,info);$('#feed-grid').append(card);}

function releaseStreamMedia(){
  for(const entry of streamPlayers.values())entry.stop();
  streamPlayers.clear();
  // Remove image URLs as well as detached players. Only metadata remains for Back.
  $('#feed-grid').querySelectorAll('.stream-card img').forEach(image=>image.removeAttribute('src'));
}
function clearStream(){
  releaseStreamMedia();
  streamIndex=0;streamNavigating=false;streamWheelTotal=0;streamGestureUntil=0;
  clearTimeout(streamWheelTimer);
  $('#feed-grid').removeAttribute('data-stream-position');
}
function renderStreamWindow(){
  if(page!=='home'||!posts[streamIndex])return;
  releaseStreamMedia();
  const grid=$('#feed-grid');
  grid.replaceChildren();
  grid.dataset.streamPosition=String(streamIndex+1);
  grid.setAttribute('aria-label','Media feed');
  grid.tabIndex=0;
  renderStreamCard(posts[streamIndex],streamIndex);
  updateSaved();
}
async function moveStream(direction){
  if(page!=='home'||streamNavigating||!posts.length)return;
  const target=streamIndex+direction;
  if(target<0){showToast('You’re at the first post.');return;}
  const token=epoch;
  streamNavigating=true;
  try{
    if(target>=posts.length&&!ended){
      if(loading)return;
      await loadPage();
    }
    if(token!==epoch||page!=='home')return;
    if(!posts[target]){
      if(ended)showToast('You’re all caught up. Explore another community for more.');
      return;
    }
    streamIndex=target;
    $('#feed-state').textContent='';
    renderStreamWindow();
  }finally{if(token===epoch)streamNavigating=false;}
}
function mountStreamVideo(entry){
  if(entry.video){entry.video.play().catch(()=>{});return;}
  const target=el('video');entry.video=target;
  target.controls=true;target.playsInline=true;target.loop=true;target.muted=true;
  target.preload='metadata';target.setAttribute('aria-label',entry.post.title);
  if(safeURL(entry.post.thumbnail))target.poster=safeURL(entry.post.thumbnail);
  entry.media.replaceChildren(target);
  const source=safeURL(entry.post.mediaUrl);
  const fail=()=>{if(entry.video!==target)return;entry.stop();entry.media.replaceChildren(external('Watch the original video ↗',entry.post.sourceUrl,'button secondary'));};
  target.addEventListener('error',fail,{once:true});
  const play=()=>{if(entry.video===target)target.play().catch(()=>{});};
  if(!source){fail();return;}
  if(/\.m3u8(?:$|\?)/i.test(source)&&window.Hls?.isSupported()){
    const player=new Hls({maxBufferLength:20});entry.hls=player;
    player.loadSource(source);player.attachMedia(target);
    player.on(Hls.Events.MANIFEST_PARSED,play);
    player.on(Hls.Events.ERROR,(_event,data)=>{if(data.fatal)fail();});
  }else{target.src=source;play();}
}
function renderStreamCard(post,index){
  const card=el('article','stream-card');card.dataset.index=String(index);
  const media=el('div','stream-media');
  const url=safeURL(post.mediaUrl);
  if(post.mediaType==='image'&&url){
    const image=el('img');image.src=url;image.alt=post.title;image.loading='eager';
    image.onerror=()=>media.replaceChildren(external('Open original post ↗',post.sourceUrl,'button secondary'));
    media.append(image);
  }else if(post.mediaType==='video'){
    const placeholder=el('div','stream-video-placeholder');
    if(safeURL(post.thumbnail)){const image=el('img');image.src=post.thumbnail;image.alt=post.title;placeholder.append(image);}
    const play=el('span','play-badge');play.append(icon('play'));placeholder.append(play);media.append(placeholder);
    const entry={post,media,video:null,hls:null,stop(){
      const player=this.hls;this.hls=null;player?.destroy();
      const old=this.video;this.video=null;
      if(old){old.pause();old.removeAttribute('src');old.load();old.remove();}
    }};
    streamPlayers.set(card,entry);
  }else{
    if(safeURL(post.thumbnail)){const image=el('img');image.src=post.thumbnail;image.alt=post.title;media.append(image);}
    media.append(external('Open original post ↗',post.sourceUrl,'button secondary'));
  }
  const caption=el('div','stream-caption');
  const sub=el('a','stream-community',`r/${post.subreddit}`);sub.href=communityLink(post.subreddit);
  const title=el('h2','',post.title);const meta=el('div','stream-meta');
  meta.append(el('span','',age(post.timestamp)),external('Original ↗',post.sourceUrl));caption.append(sub,title,meta);
  const save=button('','round-button stream-save',()=>toggleSaved(post));save.dataset.saveId=post.id;save.append(icon('bookmark'));
  card.append(media,caption,save);$('#feed-grid').append(card);
  const navigation=el('nav','stream-navigation');
  navigation.setAttribute('aria-label','Feed navigation');
  const previous=button('','round-button',()=>moveStream(-1));
  previous.setAttribute('aria-label','Previous post');previous.disabled=streamIndex===0;
  previous.append(icon('up'));
  const next=button('','round-button',()=>moveStream(1));
  next.setAttribute('aria-label','Next post');next.disabled=ended&&streamIndex===posts.length-1;
  next.append(icon('down'));navigation.append(previous,next);card.append(navigation);
  card.setAttribute('aria-label',`Post ${index+1}: ${post.title}`);
  const entry=streamPlayers.get(card);
  if(entry)mountStreamVideo(entry);
}
function renderSaved(){ended=true;posts=saved.filter(p=>kind==='all'||p.mediaType===kind);$('#feed-grid').replaceChildren();posts.forEach(renderCard);$('#feed-state').textContent='';$('#load-more').hidden=true;$('#start-swiping').disabled=!posts.length;if(!posts.length)emptyState('Keep the good ones close.','Tap the bookmark on a post and it will be waiting here. Saved posts stay in this browser.',button('Find something good','button primary',()=>navigate('#/home')));updateSaved();}
function renderExplore(){const query=searchTerm.trim().toLowerCase().replace(/^\/?r\//,'').replace(/\/$/,'');const available=new Map(communities.map(c=>[c.subreddit,c]));const all=[...communities];for(const suggestion of suggestions)if(!available.has(suggestion.subreddit))all.push({...suggestion,posts:null,videos:null});const filtered=all.filter(c=>c.subreddit.toLowerCase().includes(query));$('#community-heading').textContent=query?`Communities matching “${query}”`:'Discover communities';const grid=$('#community-grid');grid.replaceChildren();for(const c of filtered){const a=el('a','community-card');a.href=communityLink(c.subreddit);const cover=el('div','community-cover');if(safeURL(c.cover)){const img=el('img');img.src=c.cover;img.alt='';img.loading='lazy';img.onerror=()=>img.remove();cover.append(img);}const avatar=el('span','community-avatar',c.subreddit[0].toUpperCase());const body=el('div','community-body');const description=suggestions.find(s=>s.subreddit===c.subreddit)?.description||'A new corner of the internet, waiting to be explored.';body.append(el('h3','',`r/${c.subreddit}`),el('p','',description));const bottom=el('div','community-bottom');bottom.append(el('span','',c.posts===null?'Ready to discover':`${c.posts} posts · ${c.videos} videos`),el('span','','↗'));body.append(bottom);a.append(cover,avatar,body);grid.append(a);}const action=$('#search-action');action.replaceChildren();action.hidden=!query||filtered.some(c=>c.subreddit===query);if(!action.hidden){const copy=el('div');copy.append(el('h3','',`Looking for r/${query}?`),el('p','','Load a public subreddit by its exact name. Private or unavailable communities cannot be loaded.'));const valid=/^[a-z0-9_]{1,50}$/.test(query);const b=button('Explore this community','button primary',()=>navigate(communityLink(query)));b.disabled=!valid;copy.append(!valid?el('p','','Use letters, numbers, and underscores.'):el('span'));action.append(copy,b);}if(!filtered.length&&!query)grid.append(el('p','subtle','Search for a subreddit to get started.'));}
async function loadCommunity(name){const token=epoch;$('#feed-state').textContent=`Bringing r/${name} into your feed…`;$('#refresh-community').disabled=true;const buttons=$('#feed-grid').querySelectorAll('button');buttons.forEach(b=>b.disabled=true);try{await request(`/api/v1/subreddits/${encodeURIComponent(name)}/refresh`,{method:'POST'});let tries=0;async function poll(){if(token!==epoch)return;try{const result=await request(`/api/v1/subreddits/${encodeURIComponent(name)}/status`);if(token!==epoch)return;if(result.status==='ready'){$('#refresh-community').disabled=false;resetFeed();loadCommunities();showToast(`r/${name} is ready to explore.`);return;}if(result.status==='error')throw new Error(result.message||'This community could not be loaded.');if(++tries>=90)throw new Error('This is taking longer than expected. Try Refresh community in a moment.');$('#feed-state').textContent=result.status==='queued'?'Your community is next in line…':`Finding the latest from r/${name}…`;jobTimer=setTimeout(poll,2000);}catch(e){if(e.name!=='AbortError'&&token===epoch)loadError(e);}}await poll();}catch(e){if(e.name!=='AbortError'&&token===epoch)loadError(e);}function loadError(e){$('#refresh-community').disabled=false;$('#feed-state').textContent=e.message;buttons.forEach(b=>b.disabled=false);}}
function renderRoute(){clearStream();closeViewer();epoch++;controller.abort();controller=new AbortController();clearTimeout(jobTimer);const route=location.hash.slice(1)||'/home';const [path,query='']=route.split('?');page=path==='/explore'?'explore':path==='/saved'?'saved':path.startsWith('/r/')?'community':'home';try{activeFeed=page==='community'?decodeURIComponent(path.slice(3)).toLowerCase():'all';}catch{activeFeed='all';page='home';}if(!/^[a-z0-9_]{1,50}$/.test(activeFeed)){navigate('#/explore');return;}kind='all';posts=[];after=null;loading=false;ended=false;searchTerm=new URLSearchParams(query).get('q')||'';document.querySelectorAll('[data-nav]').forEach(a=>{const active=a.dataset.nav===(page==='community'?'explore':page);a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});document.querySelectorAll('[data-kind]').forEach(b=>b.classList.toggle('active',b.dataset.kind==='all'));$('#home-hero').hidden=true;document.body.classList.toggle('stream-page',page==='home');$('#explore-intro').hidden=page!=='explore';$('#explore-section').hidden=page!=='explore';$('#feed-section').hidden=page==='explore';$('#page-heading').hidden=!['saved','community'].includes(page);$('#refresh-community').hidden=page!=='community';$('#refresh-community').disabled=false;$('#breadcrumb').textContent=page==='home'?'Your daily discovery':page==='explore'?'Discover something new':page==='saved'?'Your personal collection':`Explore / r/${activeFeed}`;$('#page-eyebrow').textContent=page==='saved'?'THE GOOD ONES, KEPT CLOSE':'WELCOME TO YOUR NEXT RABBIT HOLE';$('#page-title').textContent=page==='saved'?'Saved for later.':`r/${activeFeed}`;$('#page-description').textContent=page==='saved'?'The posts you want to come back to.':'A fresh perspective from a community like no other.';$('#feed-title').textContent=page==='saved'?'Your collection':page==='community'?'From the community':'Your next good find';$('#feed-subtitle').textContent=page==='saved'?'Good taste. Great collection.':page==='community'?'Photos, videos, and everything worth a second look.':'A little bit of everything, fresh from your communities.';$('#explore-query').value=searchTerm;$('#feed-state').textContent='';document.title=page==='community'?`r/${activeFeed} — Swiiper`:`${page==='home'?'Follow your curiosity':page==='explore'?'Explore':'Saved'} — Swiiper`;window.scrollTo(0,0);renderChips();if(page==='explore')renderExplore();else if(page==='saved')renderSaved();else{skeletons();loadPage();}loadCommunities();updateSaved();}
function stopVideo(){viewerSequence++;if(video){video.pause();video.removeAttribute('src');video.load();video=null;}if(hls){hls.destroy();hls=null;}}
function closeViewer(){const dialog=$('#viewer');if(!dialog.open)return;stopVideo();dialog.close();document.body.style.overflow='';if(page==='saved')renderSaved();lastTrigger?.focus({preventScroll:true});}
function openViewer(index,trigger){if(!posts[index])return;lastTrigger=trigger||lastTrigger;viewerIndex=index;if(!$('#viewer').open){$('#viewer').showModal();document.body.style.overflow='hidden';}renderViewer();}
function renderViewer(){stopVideo();const sequence=viewerSequence;const post=posts[viewerIndex];const stage=$('#viewer-stage');stage.replaceChildren();$('#viewer-position').textContent=`${viewerIndex+1} / ${posts.length}`;$('#viewer-title').textContent=post.title;$('#viewer-community').textContent=`r/${post.subreddit}`;$('#viewer-community').href=communityLink(post.subreddit);$('#viewer-source').href=safeURL(post.sourceUrl)||'https://www.reddit.com/';$('#viewer-save').dataset.saveId=post.id;$('#viewer-save').onclick=()=>toggleSaved(post);$('#previous-post').disabled=viewerIndex===0;$('#next-post').disabled=viewerIndex===posts.length-1&&ended;updateSaved();const url=safeURL(post.mediaUrl);function fallback(message){if(sequence!==viewerSequence)return;stopVideo();const box=el('div','viewer-fallback');box.append(el('h2','','This one lives on Reddit.'),el('p','',message),external('Open original post ↗',post.sourceUrl,'button primary'));stage.replaceChildren(box);}if(post.mediaType==='video'&&url){video=el('video');const target=video;target.controls=true;target.playsInline=true;target.loop=true;target.preload='auto';target.muted=true;target.setAttribute('aria-label',post.title);if(safeURL(post.thumbnail))target.poster=safeURL(post.thumbnail);stage.append(target);const play=()=>target.play().catch(()=>{});target.addEventListener('error',()=>{console.warn('Video playback error',target.error?.code,target.error?.message);fallback('This video is unavailable here. You can still open the original post.');},{once:true});if(/\.m3u8(?:$|\?)/i.test(url)){if(window.Hls?.isSupported()){hls=new Hls({maxBufferLength:30});hls.loadSource(url);hls.attachMedia(target);hls.on(Hls.Events.MANIFEST_PARSED,play);hls.on(Hls.Events.ERROR,(_event,data)=>{if(data.fatal)fallback('The video stream could not be played. Open the original for another way to watch.');});}else if(target.canPlayType('application/vnd.apple.mpegurl')){target.src=url;play();}else fallback('This browser cannot play this video format. Try Chrome, Edge, or Safari.');}else{target.src=url;play();}}else if(post.mediaType==='image'&&url){const image=el('img');image.src=url;image.alt=post.title;image.onerror=()=>fallback('This image is no longer available here.');stage.append(image);}else fallback('Open the original post to see this content in its community.');}
async function nextPost(direction){const index=viewerIndex+direction;if(index<0)return;if(index>=posts.length&&!ended){await loadPage();}if(posts[index]){viewerIndex=index;renderViewer();}}
$('#global-search').addEventListener('submit',e=>{e.preventDefault();navigate('#/explore?q='+encodeURIComponent($('#global-query').value.trim()));});
$('#explore-search').addEventListener('submit',e=>{e.preventDefault();searchTerm=$('#explore-query').value;history.replaceState(null,'','#/explore?q='+encodeURIComponent(searchTerm));renderExplore();});
$('#explore-query').addEventListener('input',e=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{searchTerm=e.target.value;renderExplore();},150);});
document.querySelectorAll('[data-search]').forEach(b=>b.addEventListener('click',()=>navigate('#/explore?q='+encodeURIComponent(b.dataset.search))));
document.querySelectorAll('[data-kind]').forEach(b=>b.addEventListener('click',()=>setKind(b.dataset.kind)));
$('#load-more').addEventListener('click',loadPage);$('#refresh-community').addEventListener('click',()=>loadCommunity(activeFeed));
$('#start-swiping').addEventListener('click',e=>openViewer(0,e.currentTarget));$('#close-viewer').addEventListener('click',closeViewer);$('#viewer-brand').addEventListener('click',closeViewer);$('#viewer-community').addEventListener('click',closeViewer);
$('#viewer').addEventListener('cancel',e=>{e.preventDefault();closeViewer();});$('#next-post').addEventListener('click',()=>nextPost(1));$('#previous-post').addEventListener('click',()=>nextPost(-1));
let touchStart=null;$('#viewer-stage').addEventListener('touchstart',e=>{if(!e.target.closest('video'))touchStart=e.touches[0].clientY;},{passive:true});$('#viewer-stage').addEventListener('touchend',e=>{if(touchStart!==null){const delta=touchStart-e.changedTouches[0].clientY;if(Math.abs(delta)>65)nextPost(delta>0?1:-1);}touchStart=null;},{passive:true});
document.addEventListener('keydown',e=>{if(e.target.matches('input,textarea'))return;if($('#viewer').open){if(e.key==='ArrowDown'){e.preventDefault();nextPost(1);}if(e.key==='ArrowUp'){e.preventDefault();nextPost(-1);}}else if(e.key==='/'){e.preventDefault();$('#global-query').focus();}});
// A wheel/swipe changes one item; it does not move through a growing document.
$('#feed-grid').addEventListener('wheel',event=>{
  if(page!=='home'||event.ctrlKey||Math.abs(event.deltaX)>Math.abs(event.deltaY))return;
  event.preventDefault();
  const now=performance.now();
  if(now<streamGestureUntil)return;
  const delta=event.deltaY*(event.deltaMode===1?16:event.deltaMode===2?300:1);
  if(Math.sign(delta)!==Math.sign(streamWheelTotal))streamWheelTotal=0;
  streamWheelTotal+=delta;
  clearTimeout(streamWheelTimer);
  streamWheelTimer=setTimeout(()=>{streamWheelTotal=0;},140);
  if(Math.abs(streamWheelTotal)>=45){
    const direction=Math.sign(streamWheelTotal);streamWheelTotal=0;
    streamGestureUntil=now+450;
    moveStream(direction);
  }
},{passive:false});
let feedTouch=null;
$('#feed-grid').addEventListener('touchstart',event=>{
  if(page!=='home'||event.touches.length!==1||event.target.closest('button,a,input'))return;
  const touch=event.touches[0];
  // Keep native video seeking/volume controls usable.
  if(event.target.tagName==='VIDEO'&&touch.clientY>event.target.getBoundingClientRect().bottom-55)return;
  feedTouch={x:touch.clientX,y:touch.clientY};
},{passive:true});
$('#feed-grid').addEventListener('touchend',event=>{
  if(!feedTouch)return;
  const touch=event.changedTouches[0],dy=feedTouch.y-touch.clientY,dx=feedTouch.x-touch.clientX;
  feedTouch=null;
  if(Math.abs(dy)>55&&Math.abs(dy)>Math.abs(dx)*1.2)moveStream(Math.sign(dy));
},{passive:true});
$('#feed-grid').addEventListener('touchcancel',()=>{feedTouch=null;},{passive:true});
document.addEventListener('keydown',event=>{
  if(page!=='home'||$('#viewer').open||event.target.closest('input,textarea,select,video'))return;
  if(['ArrowDown','PageDown'].includes(event.key)){event.preventDefault();moveStream(1);}
  if(['ArrowUp','PageUp'].includes(event.key)){event.preventDefault();moveStream(-1);}
});
window.addEventListener('hashchange',renderRoute);document.addEventListener('visibilitychange',()=>{if(document.hidden){if(video)video.pause();for(const entry of streamPlayers.values())entry.video?.pause();}});
renderRoute();
