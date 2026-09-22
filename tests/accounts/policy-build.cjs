const path=require('path');
const root=path.resolve(__dirname,'../../frontend');
const req=require('module').createRequire(root+'/package.json');
req('esbuild').buildSync({entryPoints:[path.join(__dirname,'policy-harness.tsx')],outfile:process.argv[2],bundle:true,format:'iife',jsx:'automatic',alias:{'@':root+'/src'},nodePaths:[root+'/node_modules'],define:{'process.env.NODE_ENV':'"test"'}});
