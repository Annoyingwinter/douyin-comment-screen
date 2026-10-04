'use strict';
// Diagnostics against this app's local Electron debugging endpoint.
const fs = require('fs');
const path = require('path');
const mode = process.argv[2] || 'env';
const validationDir = path.resolve(process.argv[3] || path.join(require('os').tmpdir(), 'douyin-validation-' + Date.now()));
if (mode === 'scan') fs.mkdirSync(validationDir, { recursive: true });
const expressions = {
  env: `(async()=>{const r=await window.api.envCheck();appendConsole('app',r.ok?'环境正常: '+r.info:r.error);return r;})()`,
  login: `(async()=>{appendConsole('app','正在验证实际登录检测…');const r=await window.api.checkLogin({});if(r.result)setLoginBadge(r.result);appendConsole('app',r.ok?'检测完成: '+r.result.state:r.error);return r;})()`,
  'wait-login': `(async()=>{appendConsole('app','请在 Edge 窗口完成扫码登录…');const r=await window.api.waitLogin({timeoutSeconds:300});if(r.result)setLoginBadge(r.result);appendConsole('app',r.ok?'登录流程完成: '+r.result.state:r.error);return r;})()`,
  status: `({log:document.getElementById('console').innerText.slice(-2400),running:state.running})`,
  scan: `(async()=>{
    const original=await window.api.getConfig();
    const dir=${JSON.stringify(validationDir)};
    const params={...original.params,maxVideos:1,topicScrollRounds:2,commentScrollRounds:2,maxRunMinutes:2,readyTimeoutSeconds:45,captchaWaitSeconds:60,
      output:dir+'/summary.md',storeFile:dir+'/store.jsonl',historyFile:dir+'/history.jsonl',stopFlagFile:dir+'/stop'};
    return await new Promise(async(resolve,reject)=>{
      let done=false;
      const finish=async(result)=>{if(done)return;done=true;clearTimeout(timer);await window.api.saveConfig(original);resolve({result,validationDir:dir,log:document.getElementById('console').innerText.slice(-4000)});};
      const timer=setTimeout(()=>window.api.stopTask(),180000);
      window.api.onTaskExit(finish);
      appendConsole('app','验证采集：1 个视频，结果写入独立测试目录。');
      try{const started=await window.api.startTask(params);if(!started.ok)await finish(started);}catch(e){await finish({ok:false,error:String(e)});}
    });
  })()`,
};
async function main() {
  if (!expressions[mode]) throw new Error('Unknown mode');
  const port = fs.readFileSync(path.join(process.env.APPDATA,'douyin-screen-electron','DevToolsActivePort'),'utf8').split(/\r?\n/)[0];
  const pages = await (await fetch('http://127.0.0.1:'+port+'/json')).json();
  const target = pages.find(p=>p.type==='page'&&p.url.includes('index.html'));
  if (!target) throw new Error('App renderer not found');
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
  try {
    const result = await new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>reject(new Error('App diagnostic timeout')),360000);
      socket.addEventListener('message',event=>{
        const data=JSON.parse(event.data);
        if(data.id!==1)return;
        clearTimeout(timeout);
        if(data.error||data.result.exceptionDetails)reject(new Error(JSON.stringify(data)));
        else resolve(data.result.result.value);
      });
      socket.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{expression:expressions[mode],awaitPromise:true,returnByValue:true}}));
    });
    console.log(JSON.stringify(result,null,2));
    if(result.ok===false)process.exitCode=1;
  }finally{socket.close();}
}
main().catch(e=>{console.error(e.message);process.exitCode=1;});
