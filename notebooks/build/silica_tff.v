module XOr2 (input [1:0] I, output  O);
wire  inst0_O;
SB_LUT4 #(.LUT_INIT(16'h6666)) inst0 (.I0(I[0]), .I1(I[1]), .I2(1'b0), .I3(1'b0), .O(inst0_O));
assign O = inst0_O;
endmodule

module TFF (output  O, input  I, input  CLK);
wire  value_Q;
wire  inst1_O;
SB_DFF value (.C(CLK), .D(inst1_O), .Q(value_Q));
XOr2 inst1 (.I({value_Q,I}), .O(inst1_O));
assign O = value_Q;
endmodule

