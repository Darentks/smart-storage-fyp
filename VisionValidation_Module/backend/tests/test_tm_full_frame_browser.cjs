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
    assert.ok(html.includes('full-frame-resize-224-browser-boost-once'));
    // Inference and training export call the exact helper tested above.
    const exporter=html.slice(html.indexOf('    async function exportTMTrainingLocked('),html.indexOf('    async function executePeriodicRecheckValidation('));
    assert.ok(exporter.includes('preprocessForTM(source,stage.boost)'));
    console.log('PASS: '+file+' full-frame draw geometry for landscape/portrait/square and all boosts; shared export preprocessing');
  }
  const source=fs.readFileSync('src/components/Esp32CameraManager.jsx','utf8');
  const code=source.slice(source.indexOf('const SCENARIO_LABELS'),source.indexOf('export default function')).replace('export async function','async function');
  const labels={Front:['000','010','011','100','110','111'].map(s=>'Front_'+s),Back:['000','100','X01','11X'].map(s=>'Back_'+s)};
  let frontClass,backClass;
  const calls=[];
  const model=role=>({getClassLabels:()=>labels[role],predict:async input=>{
    assert.equal(input.width,224);assert.equal(input.height,224);
    return labels[role].map(className=>({className,probability:className===(role==='Front'?frontClass:backClass)?0.95:0.01}));
  }});
  const context={window:{}, document:{createElement:()=>({getContext:()=>({drawImage:(...args)=>calls.push(args)})})}};
  vm.createContext(context);vm.runInContext(code,context);
  const image={width:640,height:480}, models={front:model('Front'),back:model('Back')};
  assert.equal(context.modelHasRoleLabels({getClassLabels:()=>[...labels.Front,...labels.Back]},'Front'),false);
  assert.equal(context.modelHasRoleLabels(models.back,'Front'),false);
  let valid=0,conflicts=0,ambiguous=0;
  for(const front of labels.Front) for(const back of labels.Back) {
    frontClass=front;backClass=back;
    const result=await context.predictDualCamerasWithTM(image,image,models);
    if(back!=='Back_11X' && front.slice(-1)!==back.slice(-1)) {
      assert.equal(result.m2Check,'CONFLICT');assert.equal(result.totalBoxes,null);conflicts++;
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
    assert.deepEqual(calls.at(-1),[image,0,0,640,480,0,0,224,224]);
  }
  assert.equal(valid,14);assert.equal(conflicts,8);assert.equal(ambiguous,2);
  // Exercise actual pixel transforms and compare both implementations at gamma boundaries.
  const html=fs.readFileSync('index.html','utf8');
  const reference={document:{},HTMLCanvasElement:null,console};
  vm.createContext(reference);
  vm.runInContext(html.slice(html.indexOf('    function enhanceCanvasLowLight('),html.indexOf('    function renderTMDiagnostics(')),reference);
  let brightness=0, fault=null, predictions=0;
  class PixelCanvas {
    constructor(){this.width=this.height=224;this.pixels=null;}
    getContext(){
      if(fault==='null-context')return null;
      const ctx={drawImage(){},getImageData:()=>{
        if(fault==='read-throws')throw Error('pixel read denied');
        // A small uniform pixel buffer exercises identical LUT/sampling math without full-frame VM overhead.
        const data=new Uint8ClampedArray(64);
        for(let i=0;i<data.length;i+=4)data.set([brightness,brightness,brightness,255],i);
        return {data};
      },putImageData:({data})=>{
        if(fault==='write-throws')throw Error('pixel write denied');
        this.pixels=data;
      }};
      if(fault==='missing-read')delete ctx.getImageData;
      if(fault==='missing-write')delete ctx.putImageData;
      return ctx;
    }
  }
  context.document.createElement=()=>new PixelCanvas();
  reference.document.createElement=()=>new PixelCanvas();reference.HTMLCanvasElement=PixelCanvas;
  let actualPixels;
  const boostedModels=Object.fromEntries(['front','back'].map(role=>[role,{
    getClassLabels:()=>labels[role==='front'?'Front':'Back'],predict:async input=>{
      predictions++;actualPixels=input.pixels;
      return labels[role==='front'?'Front':'Back'].map(className=>({className,probability:
        className===(role==='front'?'Front_111':'Back_11X')?0.95:0.01}));
    }
  }]));
  for(brightness of [20,54,55,84,85,114,115,180])for(const boost of ['off','auto','high']) {
    actualPixels=null;
    const result=await context.predictDualCamerasWithTM(image,image,boostedModels,boost);
    assert.equal(result.status,'VERIFIED_PAIR');
    const expected=reference.preprocessForTM(image,boost).pixels;
    assert.deepEqual(actualPixels,expected);
    const mean=0.299*brightness+0.587*brightness+0.114*brightness;
    const transforms=boost==='high'||(boost==='auto'&&mean<115);
    if(transforms){
      const gamma=mean<55?0.42:mean<85?0.55:boost==='high'?0.45:0.72;
      assert.equal(actualPixels[0],Math.round(Math.pow(brightness/255,gamma)*255));
      assert.equal(actualPixels[3],255);
    }else assert.equal(actualPixels,null);
  }
  brightness=20;
  for(fault of ['missing-read','missing-write','read-throws','write-throws','null-context']) {
    for(const boost of ['auto','high']) {
      const before=predictions;
      const result=await context.predictDualCamerasWithTM(image,image,boostedModels,boost);
      assert.equal(result.status,'VALIDATION_PENDING');assert.equal(result.totalBoxes,null);
      assert.equal(predictions,before,'preprocessing failure must stop inference');
    }
  }
  fault=null;
  const before=predictions;
  assert.equal((await context.predictDualCamerasWithTM(image,image,boostedModels,'typo')).totalBoxes,null);
  assert.equal(predictions,before);
  console.log('PASS: React boost parity at gamma boundaries, off/auto/high, alpha preservation, and fail-closed pixel access');
  console.log('PASS: React helper 14 exact pairs, 2 ambiguous pairs, 8 conflicts, occlusion and full-frame resize');
})().catch(error=>{console.error(error);process.exitCode=1;});
