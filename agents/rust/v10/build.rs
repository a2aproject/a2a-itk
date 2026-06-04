fn main() {
    let proto_root = "../../../protos";
    let proto_file = format!("{proto_root}/instruction.proto");

    prost_build::Config::new()
        .compile_protos(&[&proto_file], &[proto_root])
        .expect("failed to compile instruction.proto");

    println!("cargo:rerun-if-changed={proto_root}/instruction.proto");
}
