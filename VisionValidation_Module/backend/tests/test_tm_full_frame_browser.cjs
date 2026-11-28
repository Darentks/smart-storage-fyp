const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

(async () => {
  for (const file of ['index.html','VisionValidation_Module/camera_manager.html']) {
    const html=fs.readFileSync(file,'utf8');
    const calls=[];
    class Canvas {
      constructor(){this.width=this.height=0;}
      getContext(){return {drawImage:(...args)=>calls.push(args),
        getImageData:()=>({data:new Uint8ClampedArray(this.width*this.height*4).fill(100)}),
        putImageData:()=>{}};}
    }
    const context={document:{createElement:()=>new Canvas()}, HTMLCanvasElement:Canvas, console};
    vm.createContext(context);
    vm.runInContext(html.slice(html.indexOf('    function enhanceCanvasLowLight('),html.indexOf('    function renderTMDiagnostics(')),context);
    for(const [width,height] of [[640,480],[480,640],[224,224]]) {
      for(const boost of ['off','auto','high']) {
        const image={naturalWidth:width,naturalHeight:height};
        const canvas=context.preprocessForTM(image,boost);
        assert.equal(canvas.width,224);assert.equal(canvas.height,224);
        assert.deepEqual(calls.at(-1),[image,0,0,width,height,0,0,224,224]);
        const geometry=context.tmFrameParameters(image);
        assert.equal(geometry.mode,'full-frame-resize');
        assert.equal(geometry.sourceRectWidth,width);assert.equal(geometry.sourceRectHeight,height);
      }
    }
    assert.throws(()=>context.preprocessForTM({width:0,height:0},'off'));
    // Inference and the training batch (the only training export) call the exact helper tested above.
    const batch=html.slice(html.indexOf('    async function captureTMTrainingBatch('),html.indexOf('    async function fetchLiveAnglePreviewLocked('));
    assert.ok(batch.includes("tmCanvasPNG(preprocessForTM(image,'off'))"));
    assert.ok(!/exportTMTrainingLocked|downloadTMTrainingSnapshots|compareTMPreprocessing|tmExportSweep/.test(html),'removed TM export/compare tools');
    console.log('PASS: '+file+' full-frame draw geometry for landscape/portrait/square and all boosts; training batch shares preprocessing');
  }
  // Dashboard predictDualCamerasWithTM (the unused React copy was deleted): every label pair, boost maths and
  // fail-closed preprocessing, on both dashboard files.
  const grab=(src,head)=>{const i=src.indexOf(head);assert.ok(i>=0,head);let d=0;for(let k=src.indexOf(') {',i)+2;;k++){if(src[k]==='{')d++;if(src[k]==='}'&&--d===0)return src.slice(i,k+1);}};
  for (const file of ['index.html','VisionValidation_Module/camera_manager.html']) {
    const html=fs.readFileSync(file,'utf8');
    const labels={Front:['000','010','011','100','110','111'].map(s=>'Front_'+s),Back:['000','100','X01','11X'].map(s=>'Back_'+s)};
    let frontClass,backClass,brightness=100,fault=null,predictions=0,lastInput=null;const draws=[];
    class Canvas {
      constructor(){this.width=this.height=0;this.pixels=null;}
      getContext(){
        if(fault==='null-context')return null;
        const ctx={drawImage:(...args)=>draws.push(args),getImageData:()=>{
          if(fault==='read-throws')throw Error('pixel read denied');
          // A small uniform buffer exercises the same LUT/sampling maths without full-frame VM overhead.
          const data=new Uint8ClampedArray(64);
          for(let i=0;i<data.length;i+=4)data.set([brightness,brightness,brightness,255],i);
          return {data};
        },putImageData:({data})=>{if(fault==='write-throws')throw Error('pixel write denied');this.pixels=data;}};
        if(fault==='missing-read')delete ctx.getImageData;
        if(fault==='missing-write')delete ctx.putImageData;
        return ctx;
      }
    }
    class ImageElement {}
    const model=role=>({getClassLabels:()=>labels[role],predict:async input=>{
      predictions++;lastInput=input;assert.equal(input.width,224);assert.equal(input.height,224);
      return labels[role].map(className=>({className,probability:className===(role==='Front'?frontClass:backClass)?0.95:0.01}));
    }});
    const context={window:{dualCamSettings:{confidence:0.80},tmFrontModel:model('Front'),tmBackModel:model('Back')},
      document:{createElement:()=>new Canvas()},HTMLCanvasElement:Canvas,HTMLImageElement:ImageElement,console:{warn(){}}};
    vm.createContext(context);
    vm.runInContext(html.slice(html.indexOf('    const DUAL_CAM_LABELS = {'),html.indexOf('    window.dualCamSettings'))+'\n'+
      grab(html,'    function requireDualCamModel(')+'\n'+
      html.slice(html.indexOf('    function enhanceCanvasLowLight('),html.indexOf('    function renderTMDiagnostics('))+'\n'+
      grab(html,'    async function predictDualCamerasWithTM('),context);
    const image={width:640,height:480},predict=boost=>context.predictDualCamerasWithTM(image,image,boost,{renderDiagnostics:false});
    // Combined or wrong-role models are rejected before inference.
    assert.throws(()=>context.requireDualCamModel({getClassLabels:()=>[...labels.Front,...labels.Back]},'Front'),/combined models are rejected/);
    assert.throws(()=>context.requireDualCamModel(model('Back'),'Front'),/combined models are rejected/);
    let valid=0,conflicts=0,ambiguous=0;
    for(const front of labels.Front) for(const back of labels.Back) {
      frontClass=front;backClass=back;
      const result=await predict('off');
      if(back!=='Back_11X' && front.slice(-1)!==back.slice(-1)) {
        assert.equal(result.m2Check,'CONFLICT');assert.equal(result.status,'VALIDATION_PENDING');assert.equal(result.totalBoxes,null);conflicts++;
      } else if(back==='Back_X01') {
        assert.equal(result.status,'INDETERMINATE_B1');assert.equal(result.totalBoxes,null);
        assert.equal(result.backCount,null);assert.equal(result.slots.B1,null);
        assert.equal(result.countRange.min,front==='Front_011'?2:3);
        assert.equal(result.countRange.max,result.countRange.min+1);ambiguous++;
      } else {
        assert.equal(result.status,'VERIFIED_PAIR');
        assert.equal(result.m2Check,back==='Back_11X'?'SKIPPED_OCCLUDED':'PASS');
        assert.equal(result.totalBoxes,[...front.slice(6)+back.slice(5,7)].reduce((n,b)=>n+Number(b),0));valid++;
      }
      assert.deepEqual(draws.at(-1),[image,0,0,640,480,0,0,224,224]);
    }
    assert.equal(valid,14);assert.equal(conflicts,8);assert.equal(ambiguous,2);
    // Boost maths at the gamma boundaries; pixels are written only when a boost applies.
    frontClass='Front_111';backClass='Back_11X';
    for(brightness of [20,54,55,84,85,114,115,180])for(const boost of ['off','auto','high']) {
      lastInput=null;
      const result=await predict(boost);
      assert.equal(result.status,'VERIFIED_PAIR');
      const mean=0.299*brightness+0.587*brightness+0.114*brightness;
      const transforms=boost==='high'||(boost==='auto'&&mean<115);
      if(transforms){
        const gamma=mean<55?0.42:mean<85?0.55:boost==='high'?0.45:0.72;
        assert.equal(lastInput.pixels[0],Math.round(Math.pow(brightness/255,gamma)*255));
        assert.equal(lastInput.pixels[3],255);
      }else assert.equal(lastInput.pixels,null);
    }
    // Any pixel-access failure stops inference: no prediction, quantity unknown.
    brightness=20;
    for(fault of ['missing-read','missing-write','read-throws','write-throws','null-context']) {
      for(const boost of ['auto','high']) {
        const before=predictions;
        const result=await predict(boost);
        assert.equal(result.status,'VALIDATION_PENDING');assert.equal(result.totalBoxes,null);
        assert.equal(predictions,before,'preprocessing failure must stop inference');
      }
    }
    fault=null;
    console.log('PASS: '+file+' dashboard TM helper: 14 exact pairs, 2 ranges, 8 conflicts, occlusion, full-frame resize, gamma boundaries, fail-closed pixel access');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
