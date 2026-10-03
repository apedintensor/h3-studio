// Change the view without reloading the shared project store or its cloud draft.
export function navigateWorkspace(event,path){
  if(event.defaultPrevented||event.button>0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
  if(!['/','/freestyle'].includes(path))throw Error('Unknown workspace view');
  event.preventDefault();window.history.pushState(null,'',path);window.dispatchEvent(new PopStateEvent('popstate'));window.scrollTo(0,0);
}
