const fs=require('fs'),assert=require('node:assert/strict'),vm=require('node:vm');
(async()=>{for(const file of ['index.html','VisionValidation_Module/camera_manager.html']){
 const html=fs.readFileSync(file,'utf8');let denied=false;const requests=[],toasts=[],confirmations=[];let values=[],refreshes=0;
 const context={supabaseClient:{auth:{getSession:async()=>({data:{session:{access_token:'operator-jwt'}}})}},
  prompt:()=>values.shift(),confirm:text=>{confirmations.push(text);return true;},crypto:{randomUUID:()=> 'recount-id'},
  AbortSignal,getBridgeUrl:()=>'',startupAttempts:new Set(),pollShelfReadiness:async()=>{},refreshAllData:async()=>{refreshes++;},showToast:(...args)=>toasts.push(args),
  fetch:async(url,options)=>{requests.push({url,payload:JSON.parse(options.body),headers:options.headers});
   if(url==='/reconciliation_context')return {ok:true,json:async()=>({run_id:'run',saved:{shelf_id:'s',revision:4,current_quantity:5,confirmed_occupancy:[1,1,1,1,1]},observed:{slots:[0,1,1,1,1],unverified:true}})};
   assert.equal(refreshes,0,'no optimistic refresh before commit');
   return {ok:!denied,json:async()=>denied?{error:'Operator access denied'}:{status:'accepted'}};
  }};
 vm.createContext(context);vm.runInContext(html.slice(html.indexOf('    async function operatorSession('),html.indexOf('    function readinessText(')),context);
 values=['01111','4','Physically recounted after downtime'];await context.reconcileShelf('s');
 assert.equal(requests.length,2);assert.equal(requests[1].payload.confirmed,true);assert.equal(requests[1].payload.revision,4);
 assert.equal(requests[1].headers.Authorization,'Bearer operator-jwt');assert.match(confirmations[0],/F1: 1 -> 0/);assert.equal(refreshes,1);
 requests.length=0;values=['0X111'];await context.reconcileShelf('s');assert.equal(requests.length,1);
 denied=true;refreshes=0;requests.length=0;values=['01111','4','Physical recount'];await context.reconcileShelf('s');
 assert.equal(refreshes,0);assert.match(toasts.at(-1)[0],/access denied/);
 console.log('PASS: '+file+' authenticated recount preview, explicit confirmation, partial rejection and no optimistic success on denial');
}})().catch(e=>{console.error(e);process.exitCode=1;});
